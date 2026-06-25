from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import re
import smtplib
import sys
import tempfile
import time as time_module
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email import encoders
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure
import matplotlib.pyplot as plt


CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG_EXAMPLE_FILE = Path("config.example.json")
APP_RUNTIME_DIR = Path(__file__).resolve().parent / ".aw_email_summary"
SENT_LOG_FILE = APP_RUNTIME_DIR / "sent_log.json"
APP_LOG_FILE = APP_RUNTIME_DIR / "activitywatch_email_summary.log"
DEFAULT_API_BASE_URL = "http://localhost:5600"
DEFAULT_TIMEZONE = "Europe/Berlin"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_TOP_ITEMS_LIMIT = 5
DEFAULT_WEEK_START_DAY = "mon"
SUPPORTED_TIMEFRAMES = {"daily", "weekly", "monthly", "yearly"}
WINDOW_BUCKET_HINTS = ("aw-watcher-window", "window")
AFK_BUCKET_HINTS = ("aw-watcher-afk", "afk")
WEEKDAY_BUCKETS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_BUCKETS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
WEEK_START_DAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


@dataclass(frozen=True)
class BucketCatalog:
    window_bucket_ids: tuple[str, ...]
    afk_bucket_ids: tuple[str, ...]
    category_paths: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class SMTPSettings:
    server: str
    port: int
    sender_email: str
    recipient_email: str
    username: str
    password: str
    use_tls: bool = True
    use_ssl: bool = False


@dataclass(frozen=True)
class AppConfig:
    enabled_timeframes: tuple[str, ...]
    top_items_limit: int
    lookback_days: int
    week_start_day: int
    aw_api_base_url: str
    timezone: ZoneInfo
    smtp_settings: SMTPSettings


@dataclass(frozen=True)
class WindowEvent:
    start: datetime
    end: datetime
    app: str
    title: str
    bucket_id: str


@dataclass(frozen=True)
class ReportPeriod:
    timeframe: str
    start: datetime
    end: datetime
    label: str
    key: str


@dataclass
class ReportData:
    period: ReportPeriod
    total_seconds: float
    app_seconds: dict[str, float]
    title_seconds: dict[str, float]
    category_seconds: dict[tuple[str, ...], float]
    timeline_seconds: dict[str, dict[str, float]]
    events_found: int


LOGGER = logging.getLogger("activitywatch_email_summary")


def main() -> int:
    setup_logging()
    args = parse_args()
    try:
        config = load_config(CONFIG_FILE)
    except Exception as exc:
        LOGGER.error("Fehler beim Laden der Konfiguration: %s", exc)
        if not CONFIG_FILE.exists():
            LOGGER.error(
                "Lege %s an, indem du %s kopierst und ausfüllst.",
                CONFIG_FILE,
                DEFAULT_CONFIG_EXAMPLE_FILE,
            )
        return 1

    if args.once is False:
        # Default behaviour: one boot-time run. The flag exists mainly for testability.
        pass

    time_module.sleep(15)

    try:
        sent_log = load_sent_log(SENT_LOG_FILE)
    except Exception as exc:
        LOGGER.error("Fehler beim Laden des lokalen Sent-Logs: %s", exc)
        return 1

    now = datetime.now(config.timezone)
    try:
        first_run_date, sent_log_updated = ensure_first_run_date(sent_log, now, config.timezone)
        if sent_log_updated:
            save_sent_log_atomic(SENT_LOG_FILE, sent_log)
    except Exception as exc:
        LOGGER.error("Fehler beim Initialisieren des Erststart-Cutoffs: %s", exc)
        return 1

    try:
        bucket_catalog = discover_buckets(config.aw_api_base_url)
    except Exception as exc:
        LOGGER.error("ActivityWatch-API nicht erreichbar oder nicht lesbar: %s", exc)
        return 1

    processed_any = False
    lookback_start = now - timedelta(days=config.lookback_days)
    first_run_start = datetime.combine(first_run_date, dt_time.min, tzinfo=config.timezone)
    reporting_start = max(lookback_start, first_run_start)

    for timeframe in config.enabled_timeframes:
        periods = enumerate_periods(
            timeframe,
            reporting_start,
            now,
            config.timezone,
            config.week_start_day,
        )
        for period in periods:
            if is_period_logged(sent_log, period):
                continue

            try:
                report = build_report(config, bucket_catalog, period)
                if report.total_seconds <= 0:
                    mark_period_logged(sent_log, period, has_data=False, sent=False, completed=True)
                    save_sent_log_atomic(SENT_LOG_FILE, sent_log)
                    continue

                subject, html_body, inline_images = render_email(
                    config=config,
                    report=report,
                )
                send_email(config.smtp_settings, subject, html_body, inline_images)
                mark_period_logged(sent_log, period, has_data=True, sent=True, completed=True)
                save_sent_log_atomic(SENT_LOG_FILE, sent_log)
                processed_any = True
            except Exception as exc:
                mark_period_logged(
                    sent_log,
                    period,
                    has_data=False,
                    sent=False,
                    completed=False,
                    error=str(exc),
                )
                try:
                    save_sent_log_atomic(SENT_LOG_FILE, sent_log)
                except Exception as log_exc:
                    LOGGER.error("Sent-Log konnte nach Fehler nicht geschrieben werden: %s", log_exc)
                LOGGER.exception("Fehler bei %s %s: %s", period.timeframe, period.label, exc)
                continue

    if not processed_any:
        LOGGER.info("Keine neuen Berichte zu versenden.")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ActivityWatch E-Mail Report Generator")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Führt genau einen Lauf aus. Standardverhalten.",
    )
    return parser.parse_args()


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} fehlt. Kopiere {DEFAULT_CONFIG_EXAMPLE_FILE} nach {path} und trage echte Werte ein."
        )

    raw = json.loads(path.read_text(encoding="utf-8"))

    enabled_raw = raw.get("enabled_timeframes", [])
    if not isinstance(enabled_raw, list):
        raise ValueError("enabled_timeframes muss eine Liste sein.")
    enabled_timeframes = tuple(normalize_timeframe(value) for value in enabled_raw)
    if not enabled_timeframes:
        raise ValueError("enabled_timeframes darf nicht leer sein.")

    invalid_timeframes = [value for value in enabled_timeframes if value not in SUPPORTED_TIMEFRAMES]
    if invalid_timeframes:
        raise ValueError(
            f"Ungültige Timeframes: {invalid_timeframes}. Erlaubt sind: {sorted(SUPPORTED_TIMEFRAMES)}"
        )

    top_items_limit = int(raw.get("top_items_limit", DEFAULT_TOP_ITEMS_LIMIT))
    if top_items_limit < 1:
        raise ValueError("top_items_limit muss >= 1 sein.")

    lookback_days = int(raw.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    if lookback_days < 1:
        raise ValueError("lookback_days muss >= 1 sein.")

    week_start_day = parse_week_start_day(raw.get("week_start_day", DEFAULT_WEEK_START_DAY))

    aw_api_base_url = str(raw.get("aw_api_base_url", DEFAULT_API_BASE_URL)).rstrip("/")
    timezone_name = str(raw.get("timezone", DEFAULT_TIMEZONE))
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Ungültige Zeitzone: {timezone_name}") from exc

    smtp_settings = parse_smtp_settings(raw.get("smtp_settings", {}))

    return AppConfig(
        enabled_timeframes=enabled_timeframes,
        top_items_limit=top_items_limit,
        lookback_days=lookback_days,
        week_start_day=week_start_day,
        aw_api_base_url=aw_api_base_url,
        timezone=tz,
        smtp_settings=smtp_settings,
    )


def parse_smtp_settings(raw: dict[str, Any]) -> SMTPSettings:
    required = ["server", "port", "sender_email", "recipient_email", "username", "password"]
    missing = [field for field in required if field not in raw or raw[field] in ("", None)]
    if missing:
        raise ValueError(f"smtp_settings fehlen Pflichtfelder: {missing}")

    return SMTPSettings(
        server=str(raw["server"]),
        port=int(raw["port"]),
        sender_email=str(raw["sender_email"]),
        recipient_email=str(raw["recipient_email"]),
        username=str(raw["username"]),
        password=str(raw["password"]),
        use_tls=bool(raw.get("use_tls", True)),
        use_ssl=bool(raw.get("use_ssl", False)),
    )


def normalize_timeframe(value: str) -> str:
    return str(value).strip().lower()


def parse_week_start_day(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 6:
            return value
        raise ValueError("week_start_day muss zwischen 0 und 6 liegen.")

    normalized = str(value).strip().lower()
    if normalized.isdigit():
        numeric = int(normalized)
        if 0 <= numeric <= 6:
            return numeric
        raise ValueError("week_start_day muss zwischen 0 und 6 liegen.")

    if normalized in WEEK_START_DAYS:
        return WEEK_START_DAYS[normalized]

    raise ValueError(
        "week_start_day muss einer von mon, tue, wed, thu, fri, sat, sun oder eine Zahl 0-6 sein."
    )


def normalize_path_segment(value: Any) -> str:
    text = str(value).strip()
    return text if text else "Uncategorized"


def setup_logging() -> None:
    APP_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    file_handler = logging.FileHandler(APP_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)

    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(stream_handler)


def load_sent_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"reports": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"reports": {}}
    if not isinstance(data, dict):
        return {"reports": {}}
    if not isinstance(data.get("reports"), dict):
        data["reports"] = {}
    return data


def ensure_first_run_date(sent_log: dict[str, Any], now: datetime, tz: ZoneInfo) -> tuple[date, bool]:
    raw_value = sent_log.get("first_run_date")
    if isinstance(raw_value, str):
        try:
            return date.fromisoformat(raw_value), False
        except ValueError:
            pass

    first_run_date = now.astimezone(tz).date()
    sent_log["first_run_date"] = first_run_date.isoformat()
    return first_run_date, True


def save_sent_log_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False, sort_keys=True)
        tmp.flush()
        os.fsync(tmp.fileno())
        temp_name = tmp.name
    os.replace(temp_name, path)


def is_period_logged(sent_log: dict[str, Any], period: ReportPeriod) -> bool:
    return (
        sent_log.get("reports", {})
        .get(period.timeframe, {})
        .get(period.key, {})
        .get("completed", False)
    )


def mark_period_logged(
    sent_log: dict[str, Any],
    period: ReportPeriod,
    *,
    has_data: bool,
    sent: bool,
    completed: bool,
    error: str | None = None,
) -> None:
    reports = sent_log.setdefault("reports", {})
    by_timeframe = reports.setdefault(period.timeframe, {})
    by_timeframe[period.key] = {
        "timeframe": period.timeframe,
        "label": period.label,
        "start": period.start.isoformat(),
        "end": period.end.isoformat(),
        "completed": completed,
        "has_data": has_data,
        "sent": sent,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }


def enumerate_periods(
    timeframe: str,
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    week_start_day: int = 0,
) -> list[ReportPeriod]:
    timeframe = normalize_timeframe(timeframe)
    periods: list[ReportPeriod] = []
    current_start = align_start_to_timeframe(start, timeframe, tz, week_start_day)
    while current_start < end:
        current_end = add_timeframe(current_start, timeframe, tz)
        if current_end <= start:
            current_start = current_end
            continue
        clipped_start = max(current_start, start)
        clipped_end = min(current_end, end)
        key, label = build_period_metadata(current_start, timeframe, tz)
        periods.append(
            ReportPeriod(
                timeframe=timeframe,
                start=clipped_start,
                end=clipped_end,
                label=label,
                key=key,
            )
        )
        current_start = current_end
    return periods


def align_start_to_timeframe(
    value: datetime,
    timeframe: str,
    tz: ZoneInfo,
    week_start_day: int = 0,
) -> datetime:
    localized = value.astimezone(tz)
    if timeframe == "daily":
        return localized.replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe == "weekly":
        start_of_day = localized.replace(hour=0, minute=0, second=0, microsecond=0)
        offset = (start_of_day.weekday() - week_start_day) % 7
        return start_of_day - timedelta(days=offset)
    if timeframe == "monthly":
        return localized.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if timeframe == "yearly":
        return localized.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def add_timeframe(start: datetime, timeframe: str, tz: ZoneInfo) -> datetime:
    if timeframe == "daily":
        return start + timedelta(days=1)
    if timeframe == "weekly":
        return start + timedelta(days=7)
    if timeframe == "monthly":
        year = start.year + (1 if start.month == 12 else 0)
        month = 1 if start.month == 12 else start.month + 1
        return start.replace(year=year, month=month, day=1)
    if timeframe == "yearly":
        return start.replace(year=start.year + 1)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def build_period_metadata(start: datetime, timeframe: str, tz: ZoneInfo) -> tuple[str, str]:
    localized = start.astimezone(tz)
    if timeframe == "daily":
        return localized.date().isoformat(), localized.strftime("%Y-%m-%d")
    if timeframe == "weekly":
        key = localized.date().isoformat()
        return key, key
    if timeframe == "monthly":
        key = localized.strftime("%Y-%m")
        return key, key
    if timeframe == "yearly":
        return str(localized.year), str(localized.year)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def discover_buckets(api_base_url: str) -> BucketCatalog:
    buckets = api_get_json(api_base_url, "/api/0/buckets")
    window_bucket_ids = []
    afk_bucket_ids = []
    category_paths: dict[str, tuple[str, ...]] = {}
    for bucket_id in buckets.keys():
        if any(hint in bucket_id.lower() for hint in WINDOW_BUCKET_HINTS):
            window_bucket_ids.append(bucket_id)
            category_paths[bucket_id] = derive_category_path(bucket_id, buckets.get(bucket_id, {}))
        if any(hint in bucket_id.lower() for hint in AFK_BUCKET_HINTS):
            afk_bucket_ids.append(bucket_id)

    if not window_bucket_ids:
        raise RuntimeError("Kein ActivityWatch-Window-Bucket gefunden.")

    return BucketCatalog(
        window_bucket_ids=tuple(sorted(window_bucket_ids)),
        afk_bucket_ids=tuple(sorted(afk_bucket_ids)),
        category_paths=category_paths,
    )


def api_get_json(api_base_url: str, path: str, params: dict[str, Any] | None = None) -> Any:
    query = f"?{urlencode(params or {}, doseq=True)}" if params else ""
    url = f"{api_base_url}{path}{query}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=30) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} bei {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Netzwerkfehler bei {url}: {exc.reason}") from exc


def derive_category_path(bucket_id: str, bucket: dict[str, Any]) -> tuple[str, ...]:
    raw_value: Any = bucket.get("category")
    if raw_value is None:
        data = bucket.get("data", {})
        if isinstance(data, dict):
            raw_value = data.get("category")
    if raw_value is None:
        raw_value = bucket.get("name") or bucket_id

    if isinstance(raw_value, (list, tuple)):
        parts = [normalize_path_segment(part) for part in raw_value if str(part).strip()]
    else:
        parts = [
            normalize_path_segment(part)
            for part in re.split(r"\s*(?:/|>|\\|\|)\s*", str(raw_value))
            if str(part).strip()
        ]

    return tuple(parts) if parts else (normalize_path_segment(bucket_id),)


def build_report(config: AppConfig, bucket_catalog: BucketCatalog, period: ReportPeriod) -> ReportData:
    window_events = fetch_window_events(config, list(bucket_catalog.window_bucket_ids), period)
    active_intervals = fetch_active_intervals(config, list(bucket_catalog.afk_bucket_ids), period)

    app_seconds: dict[str, float] = defaultdict(float)
    title_seconds: dict[str, float] = defaultdict(float)
    category_seconds: dict[tuple[str, ...], float] = defaultdict(float)
    timeline_seconds: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for event in window_events:
        clipped_start = max(event.start, period.start)
        clipped_end = min(event.end, period.end)
        if clipped_end <= clipped_start:
            continue
        seconds = (
            overlap_seconds_with_intervals(clipped_start, clipped_end, active_intervals)
            if active_intervals
            else event_duration_seconds(clipped_start, clipped_end)
        )
        if seconds <= 0:
            continue
        app_key = event.app or "Unknown"
        title_key = event.title or "Unknown"
        category_path = resolve_category_path(bucket_catalog, event.bucket_id)
        app_seconds[app_key] += seconds
        title_seconds[title_key] += seconds
        category_seconds[category_path] += seconds
        timeline_bucket = get_timeline_bucket(period, clipped_start, config.timezone)
        timeline_seconds[timeline_bucket][category_root(category_path)] += seconds

    total_seconds = sum(app_seconds.values())
    return ReportData(
        period=period,
        total_seconds=total_seconds,
        app_seconds=dict(app_seconds),
        title_seconds=dict(title_seconds),
        category_seconds=dict(category_seconds),
        timeline_seconds={k: dict(v) for k, v in timeline_seconds.items()},
        events_found=len(window_events),
    )


def fetch_window_events(config: AppConfig, bucket_ids: list[str], period: ReportPeriod) -> list[WindowEvent]:
    events: list[WindowEvent] = []
    for bucket_id in bucket_ids:
        raw_events = api_get_json(
            config.aw_api_base_url,
            f"/api/0/buckets/{bucket_id}/events",
            params={
                "start": period.start.astimezone(timezone.utc).isoformat(),
                "end": period.end.astimezone(timezone.utc).isoformat(),
            },
        )
        for raw in raw_events:
            try:
                start = parse_aw_timestamp(raw["timestamp"], config.timezone)
                duration = float(raw.get("duration", 0))
                end = start + timedelta(seconds=duration)
                data = raw.get("data", {})
                events.append(
                    WindowEvent(
                        start=start,
                        end=end,
                        app=str(data.get("app", "")),
                        title=str(data.get("title", "")),
                        bucket_id=bucket_id,
                    )
                )
            except Exception:
                continue
    events.sort(key=lambda item: item.start)
    return events


def fetch_active_intervals(config: AppConfig, bucket_ids: list[str], period: ReportPeriod) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    for bucket_id in bucket_ids:
        raw_events = api_get_json(
            config.aw_api_base_url,
            f"/api/0/buckets/{bucket_id}/events",
            params={
                "start": period.start.astimezone(timezone.utc).isoformat(),
                "end": period.end.astimezone(timezone.utc).isoformat(),
            },
        )
        normalized = []
        for raw in raw_events:
            try:
                start = parse_aw_timestamp(raw["timestamp"], config.timezone)
                status = str(raw.get("data", {}).get("status", "")).lower()
                normalized.append((start, status))
            except Exception:
                continue
        normalized.sort(key=lambda item: item[0])
        for idx, (start, status) in enumerate(normalized):
            end = normalized[idx + 1][0] if idx + 1 < len(normalized) else period.end
            if status not in {"afk", "inactive"}:
                intervals.append((start, end))
    return merge_intervals(intervals)


def fetch_afk_intervals(config: AppConfig, bucket_ids: list[str], period: ReportPeriod) -> list[tuple[datetime, datetime]]:
    return fetch_active_intervals(config, bucket_ids, period)


def parse_aw_timestamp(value: str, tz: ZoneInfo) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz)


def event_duration_seconds(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def overlap_seconds_with_intervals(
    start: datetime,
    end: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> float:
    total = 0.0
    for interval_start, interval_end in intervals:
        overlap_start = max(start, interval_start)
        overlap_end = min(end, interval_end)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds()
    return total


def merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: item[0])
    merged = [sorted_intervals[0]]
    for start, end in sorted_intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def resolve_category_path(bucket_catalog: BucketCatalog, bucket_id: str) -> tuple[str, ...]:
    return bucket_catalog.category_paths.get(bucket_id, (normalize_path_segment(bucket_id),))


def category_root(path: tuple[str, ...]) -> str:
    return path[0] if path else "Uncategorized"


def get_timeline_bucket(period: ReportPeriod, moment: datetime, tz: ZoneInfo) -> str:
    localized = moment.astimezone(tz)
    if period.timeframe == "daily":
        return f"{localized.hour:02d}:00"
    if period.timeframe == "weekly":
        return WEEKDAY_BUCKETS[localized.weekday()]
    if period.timeframe == "monthly":
        return f"{localized.day:02d}"
    if period.timeframe == "yearly":
        return MONTH_BUCKETS[localized.month - 1]
    return localized.strftime("%Y-%m-%d")


def render_email(config: AppConfig, report: ReportData) -> tuple[str, str, list[tuple[str, bytes]]]:
    images = generate_report_images(config, report)
    subject = f"ActivityWatch {report.period.timeframe} report - {report.period.label}"
    html = build_html_email(config, report, images)
    return subject, html, images


def generate_report_images(config: AppConfig, report: ReportData) -> list[tuple[str, bytes]]:
    temp_paths: list[Path] = []
    try:
        top_apps = top_n_items(report.app_seconds, config.top_items_limit)
        top_titles = top_n_items(report.title_seconds, config.top_items_limit)
        category_plot = create_category_plot(report.category_seconds)
        timeline_plot = create_timeline_plot(report, config.top_items_limit, config.week_start_day)
        app_plot = create_horizontal_bar_chart(top_apps, "Top Applications")
        title_plot = create_horizontal_bar_chart(top_titles, "Top Window Titles")

        plots = [
            ("top-apps", app_plot),
            ("top-titles", title_plot),
            ("category", category_plot),
            ("timeline", timeline_plot),
        ]
        images: list[tuple[str, bytes]] = []
        for cid, fig in plots:
            fd, tmp_name = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            tmp_path = Path(tmp_name)
            fig.savefig(tmp_path, bbox_inches="tight", dpi=160)
            temp_paths.append(tmp_path)
            images.append((cid, tmp_path.read_bytes()))
            plt.close(fig)
        return images
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


def top_n_items(values: dict[str, float], limit: int) -> list[tuple[str, float]]:
    return sorted(values.items(), key=lambda item: item[1], reverse=True)[:limit]


def create_horizontal_bar_chart(items: list[tuple[str, float]], title: str) -> Figure:
    fig, ax = plt.subplots(figsize=(7.0, max(2.6, 0.75 * max(1, len(items)) + 0.9)))
    if not items:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", fontsize=12)
        ax.set_axis_off()
        return fig

    labels = [label[:48] for label, _ in items]
    values = [seconds_to_hours(seconds) for _, seconds in items]
    y_pos = list(range(len(items)))
    palette = [
        "#bfbfbf",
        "#67d0ff",
        "#0b72c6",
        "#bbe500",
        "#80d8ff",
        "#f7a600",
        "#7e57c2",
        "#4caf50",
    ]
    colors = [palette[idx % len(palette)] for idx in range(len(items))]
    ax.barh(y_pos, values, color=colors, height=0.88, edgecolor="none")
    ax.set_yticks(y_pos)
    ax.set_yticklabels([""] * len(items))
    ax.invert_yaxis()
    max_value = max(values) or 1.0
    ax.set_xlim(0, max_value * 1.18)
    ax.set_title(title, loc="left", fontsize=14, pad=10)
    ax.tick_params(axis="x", bottom=False, labelbottom=False)
    ax.tick_params(axis="y", left=False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    x_offset = max_value * 0.02
    for idx, value in enumerate(values):
        ax.text(
            x_offset,
            idx - 0.14,
            labels[idx],
            va="center",
            ha="left",
            fontsize=10.5,
            color="#1f2937",
            clip_on=True,
        )
        ax.text(
            x_offset,
            idx + 0.17,
            format_duration_compact(items[idx][1]),
            va="center",
            ha="left",
            fontsize=8.3,
            color="#1f2937",
            clip_on=True,
        )
    fig.tight_layout()
    return fig


def create_category_plot(category_seconds: dict[tuple[str, ...], float]) -> Figure:
    fig, ax = plt.subplots(figsize=(7.2, 7.2))
    if not category_seconds:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", fontsize=14)
        ax.set_axis_off()
        return fig

    leaf_items = sorted(category_seconds.items(), key=lambda item: item[1], reverse=True)
    inner_totals: dict[str, float] = defaultdict(float)
    outer_labels: list[str] = []
    outer_sizes: list[float] = []
    outer_colors: list[tuple[float, float, float, float]] = []
    palette = plt.get_cmap("tab20")

    for index, (path, seconds) in enumerate(leaf_items):
        outer_labels.append(" > ".join(path))
        outer_sizes.append(seconds)
        outer_colors.append(palette(index % 20))
        inner_totals[path[0]] += seconds

    inner_labels = list(inner_totals.keys())
    inner_sizes = [inner_totals[label] for label in inner_labels]
    inner_colors = [palette(i) for i in range(len(inner_labels))]

    ax.pie(
        inner_sizes,
        labels=inner_labels,
        radius=1.0,
        wedgeprops=dict(width=0.28, edgecolor="white"),
        autopct=lambda pct: f"{pct:.1f}%" if pct >= 8 else "",
        startangle=90,
        colors=inner_colors,
        textprops={"fontsize": 8},
    )
    ax.pie(
        outer_sizes,
        labels=outer_labels,
        radius=1.0 - 0.28,
        labeldistance=0.78,
        wedgeprops=dict(width=0.28, edgecolor="white"),
        startangle=90,
        colors=outer_colors,
        textprops={"fontsize": 7.5},
    )
    ax.set_title("Category Sunburst", loc="left", fontsize=15, pad=12)
    return fig


def create_timeline_plot(report: ReportData, top_items_limit: int, week_start_day: int = 0) -> Figure:
    fig, ax = plt.subplots(figsize=(7.4, 5.8))
    if not report.timeline_seconds:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", fontsize=12)
        ax.set_axis_off()
        return fig

    buckets = ordered_timeline_buckets(report.period.timeframe, week_start_day)
    category_names = sorted(
        {
            category
            for bucket_values in report.timeline_seconds.values()
            for category in bucket_values.keys()
        }
    )
    totals = defaultdict(float)
    for bucket_values in report.timeline_seconds.values():
        for category, seconds in bucket_values.items():
            totals[category] += seconds
    top_categories = [name for name, _ in sorted(totals.items(), key=lambda item: item[1], reverse=True)[:top_items_limit]]
    other_label = "Other"

    stack_categories = top_categories
    if len(category_names) > len(top_categories):
        stack_categories = top_categories + [other_label]

    cmap = plt.get_cmap("tab20")
    bottom = [0.0] * len(buckets)
    for idx, category in enumerate(stack_categories):
        heights = []
        for bucket in buckets:
            bucket_values = report.timeline_seconds.get(bucket, {})
            if category == other_label:
                height = sum(
                    seconds for name, seconds in bucket_values.items() if name not in top_categories
                )
            else:
                height = bucket_values.get(category, 0.0)
            heights.append(height / 60.0)
        ax.bar(buckets, heights, bottom=bottom, color=cmap(idx % 20), width=0.8, label=category)
        bottom = [b + h for b, h in zip(bottom, heights)]

    ax.set_ylabel("")
    ax.set_title("Timeline (barchart)", loc="left", fontsize=15, pad=12)
    ax.grid(True, axis="both", linestyle="-", color="#d7d7d7", alpha=0.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(axis="x", rotation=45, labelsize=9)
    ax.tick_params(axis="y", labelsize=9)
    if len(buckets) > 1:
        step = 2 if report.period.timeframe == "daily" else max(1, len(buckets) // 8)
        tick_positions = list(range(0, len(buckets), step))
        ax.set_xticks(tick_positions, [buckets[i] for i in tick_positions])
    ax.set_yticks([0, 15, 30, 45, 60, 75], ["0", "15m", "30m", "45m", "1h", "1.25h"])
    fig.tight_layout()
    return fig


def ordered_timeline_buckets(timeframe: str, week_start_day: int = 0) -> list[str]:
    if timeframe == "daily":
        return [f"{hour:02d}:00" for hour in range(24)]
    if timeframe == "weekly":
        return ordered_weekday_buckets(week_start_day)
    if timeframe == "monthly":
        return [f"{day:02d}" for day in range(1, 32)]
    if timeframe == "yearly":
        return MONTH_BUCKETS
    return []


def ordered_weekday_buckets(week_start_day: int | str) -> list[str]:
    if isinstance(week_start_day, str):
        week_start_day = parse_week_start_day(week_start_day)
    return WEEKDAY_BUCKETS[week_start_day:] + WEEKDAY_BUCKETS[:week_start_day]


def seconds_to_hours(seconds: float) -> float:
    return seconds / 3600.0


def build_html_email(config: AppConfig, report: ReportData, _images: list[tuple[str, bytes]]) -> str:
    total_time = format_seconds(report.total_seconds)
    category_tree_html = build_category_tree_html(report.category_seconds)
    top_categories_html = build_bar_list_html(
        report.category_seconds,
        config.top_items_limit,
        item_formatter=lambda path: " > ".join(path),
    )
    top_apps_more_html = '<div class="show-more">⌄⌄ Show more</div>' if len(report.app_seconds) > config.top_items_limit else ""
    top_titles_more_html = '<div class="show-more">⌄⌄ Show more</div>' if len(report.title_seconds) > config.top_items_limit else ""

    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <style>
          body {{
            font-family: Arial, sans-serif;
            color: #243041;
            line-height: 1.45;
            background: #ffffff;
          }}
          .wrap {{
            max-width: 1180px;
            margin: 0 auto;
            padding: 16px 18px 28px;
          }}
          .header {{
            margin-bottom: 14px;
          }}
          .header h1 {{
            margin: 0;
            font-size: 28px;
            font-weight: 700;
          }}
          .summary {{
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
            margin-top: 8px;
            color: #5b6574;
            font-size: 13px;
          }}
          .card {{
            background: #fff;
            border: 1px solid #e6e8ee;
            border-radius: 10px;
            padding: 12px 12px 14px;
            box-sizing: border-box;
            overflow: hidden;
          }}
          .card h2 {{
            margin: 0 0 10px;
            font-size: 20px;
            font-weight: 500;
            color: #243041;
          }}
          .dashboard-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px 18px;
            align-items: start;
          }}
          .dashboard-grid .card img {{
            margin-top: 0;
          }}
          .metric-list {{
            display: flex;
            flex-direction: column;
            gap: 8px;
          }}
          .metric-item {{
            --bar-color: #87cefa;
            --bar-width: 100%;
            width: var(--bar-width);
            min-width: 120px;
            background: var(--bar-color);
            border-radius: 5px;
            padding: 6px 7px 5px;
            color: #20252d;
            box-sizing: border-box;
            box-shadow: inset 0 -1px 0 rgba(0, 0, 0, 0.04);
          }}
          .metric-label {{
            font-size: 14px;
            line-height: 1.1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }}
          .metric-duration {{
            font-size: 11px;
            margin-top: 2px;
          }}
          .show-more {{
            margin-top: 8px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            border: 1px solid #97a2b4;
            border-radius: 4px;
            padding: 4px 10px;
            color: #4a5a72;
            background: #fff;
            font-size: 13px;
            width: fit-content;
          }}
          .muted {{
            color: #6b7280;
          }}
          .tree-list {{
            display: flex;
            flex-direction: column;
            gap: 7px;
          }}
          .tree-entry {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 10px;
            padding-left: calc(var(--depth, 0) * 16px);
          }}
          .tree-main {{
            display: flex;
            align-items: center;
            gap: 5px;
            min-width: 0;
          }}
          .tree-marker {{
            color: #7a8493;
            font-size: 13px;
            line-height: 1;
            flex: 0 0 auto;
          }}
          .tree-name {{
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }}
          .tree-duration {{
            color: #4a5568;
            white-space: nowrap;
            margin-left: 12px;
            flex: 0 0 auto;
          }}
          .tree-percent {{
            margin-left: 8px;
            color: #7a8493;
            font-size: 12px;
          }}
          .tree-footnote {{
            margin-top: 12px;
            padding-top: 12px;
            border-top: 1px solid #e6e8ee;
          }}
          .checkbox {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            font-size: 13px;
            color: #4a5568;
          }}
          img {{
            max-width: 100%;
            height: auto;
            display: block;
          }}
          @media (max-width: 1080px) {{
            .dashboard-grid {{
              grid-template-columns: repeat(2, minmax(0, 1fr));
            }}
          }}
          @media (max-width: 720px) {{
            .dashboard-grid {{
              grid-template-columns: 1fr;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="header">
            <h1>ActivityWatch {escape_html(report.period.timeframe.title())} Report</h1>
            <div class="summary">
              <div><strong>Zeitraum:</strong> {escape_html(report.period.label)}</div>
              <div><strong>Gesamtzeit:</strong> {escape_html(total_time)}</div>
              <div><strong>Gefundene Events:</strong> {report.events_found}</div>
            </div>
          </div>

          <div class="dashboard-grid">
            <section class="card">
              <h2>Top Applications</h2>
              <img src="cid:top-apps" alt="Top Applications" />
              {top_apps_more_html}
            </section>
            <section class="card">
              <h2>Top Window Titles</h2>
              <img src="cid:top-titles" alt="Top Window Titles" />
              {top_titles_more_html}
            </section>
            <section class="card">
              <h2>Timeline (barchart)</h2>
              <img src="cid:timeline" alt="Timeline" />
            </section>
            <section class="card">
              <h2>Top Categories</h2>
              {top_categories_html}
            </section>
            <section class="card">
              <h2>Category Tree</h2>
              {category_tree_html}
              <div class="tree-footnote">
                <label class="checkbox"><input type="checkbox" /> Show percent</label>
              </div>
            </section>
            <section class="card">
              <h2>Category Sunburst</h2>
              <img src="cid:category" alt="Category Sunburst" />
            </section>
          </div>
        </div>
      </body>
    </html>
    """
    return html


def escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


def build_bar_list_html(
    values: dict[Any, float],
    limit: int,
    item_formatter: Any | None = None,
) -> str:
    rows = top_n_items(values, limit)
    if not rows:
        return "<p class='muted'>Keine Daten</p>"
    max_seconds = max((seconds for _, seconds in rows), default=1.0) or 1.0
    palette = [
        "#67d0ff",
        "#bfc0c0",
        "#0b72c6",
        "#bbe500",
        "#ffb000",
        "#80d8ff",
        "#6fcf97",
        "#b39ddb",
    ]
    html_rows = ['<div class="metric-list">']
    for label, seconds in rows:
        rendered_label = item_formatter(label) if item_formatter else label
        width = max(16.0, seconds / max_seconds * 100.0)
        color = palette[len(html_rows) % len(palette)]
        html_rows.append(
            "<div class=\"metric-item\" "
            f"style=\"--bar-width: {width:.1f}%; --bar-color: {color};\">"
            f"<div class=\"metric-label\">{escape_html(str(rendered_label))}</div>"
            f"<div class=\"metric-duration\">{escape_html(format_duration_compact(seconds))}</div>"
            "</div>"
        )
    if len(rows) < len(values):
        html_rows.append('<div class="show-more">⌄⌄ Show more</div>')
    html_rows.append("</div>")
    return "".join(html_rows)


def build_category_tree_html(category_seconds: dict[tuple[str, ...], float]) -> str:
    if not category_seconds:
        return "<p class='muted'>Keine Daten</p>"

    tree: dict[str, Any] = {}
    totals: dict[tuple[str, ...], float] = defaultdict(float)

    for path, seconds in category_seconds.items():
        for idx in range(1, len(path) + 1):
            totals[path[:idx]] += seconds
        node = tree
        for segment in path[:-1]:
            node = node.setdefault(segment, {})
        node.setdefault(path[-1], {})

    total_seconds = sum(category_seconds.values()) or 1.0

    def render_node(node: dict[str, Any], prefix: tuple[str, ...], depth: int = 0) -> str:
        items = []
        for name in sorted(node.keys()):
            current_path = prefix + (name,)
            seconds = totals.get(current_path, 0.0)
            percent = seconds / total_seconds * 100
            children = node[name]
            children_html = render_node(children, current_path, depth + 1) if children else ""
            items.append(
                f'<div class="tree-entry" style="--depth: {depth};">'
                f'<div class="tree-main"><span class="tree-marker">⊞</span><span class="tree-name">{escape_html(name)}</span></div>'
                f'<div class="tree-duration">{escape_html(format_duration_compact(seconds))}</div>'
                f'<span class="tree-percent">{percent:.1f}%</span>'
                f"</div>"
                f"{children_html}"
            )
        return f'<div class="tree-list">{"" .join(items)}</div>' if items else ""

    return render_node(tree, ())


def format_duration_compact(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if minutes or hours:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def format_seconds(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def send_email(smtp_settings: SMTPSettings, subject: str, html_body: str, images: list[tuple[str, bytes]]) -> None:
    message = MIMEMultipart("related")
    message["Subject"] = subject
    message["From"] = smtp_settings.sender_email
    message["To"] = smtp_settings.recipient_email

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(strip_html_tags(html_body), "plain", "utf-8"))
    alternative.attach(MIMEText(html_body, "html", "utf-8"))
    message.attach(alternative)

    for cid, payload in images:
        image = MIMEImage(payload, _subtype="png")
        image.add_header("Content-ID", f"<{cid}>")
        image.add_header("Content-Disposition", "inline", filename=f"{cid}.png")
        message.attach(image)

    if smtp_settings.use_ssl:
        with smtplib.SMTP_SSL(smtp_settings.server, smtp_settings.port, timeout=30) as client:
            client.login(smtp_settings.username, smtp_settings.password)
            client.send_message(message)
        return

    with smtplib.SMTP(smtp_settings.server, smtp_settings.port, timeout=30) as client:
        client.ehlo()
        if smtp_settings.use_tls:
            client.starttls()
            client.ehlo()
        client.login(smtp_settings.username, smtp_settings.password)
        client.send_message(message)


def strip_html_tags(html: str) -> str:
    text = re.sub(r"<style.*?</style>", "", html, flags=re.S | re.I)
    text = re.sub(r"<script.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


if __name__ == "__main__":
    raise SystemExit(main())
