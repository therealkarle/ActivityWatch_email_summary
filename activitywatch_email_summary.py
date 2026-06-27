from __future__ import annotations

import argparse
import dataclasses
import json
import math
import logging
import os
import re
import smtplib
import sys
import tempfile
import time as time_module
import textwrap
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
from matplotlib.patches import Wedge
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
DEFAULT_CATEGORY_NAME = "Uncategorized"
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
    category_path: tuple[str, ...] | None = None


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
        LOGGER.error("Error loading configuration: %s", exc)
        if not CONFIG_FILE.exists():
            LOGGER.error(
                "Create %s by copying %s and filling it in.",
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
        LOGGER.error("Error loading local sent log: %s", exc)
        return 1

    now = datetime.now(config.timezone)
    try:
        first_run_date, sent_log_updated = ensure_first_run_date(sent_log, now, config.timezone)
        if sent_log_updated:
            save_sent_log_atomic(SENT_LOG_FILE, sent_log)
    except Exception as exc:
        LOGGER.error("Error initializing first-run cutoff: %s", exc)
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
    daily_reporting_start = max(lookback_start, first_run_start - timedelta(days=1))
    daily_reporting_end = align_start_to_timeframe(now, "daily", config.timezone)

    for timeframe in config.enabled_timeframes:
        if timeframe == "daily":
            periods = enumerate_periods(
                timeframe,
                daily_reporting_start,
                daily_reporting_end,
                config.timezone,
                config.week_start_day,
            )
        else:
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
                    LOGGER.error("Could not write sent log after error: %s", log_exc)
                LOGGER.exception("Error while processing %s %s: %s", period.timeframe, period.label, exc)
                continue

    if not processed_any:
        LOGGER.info("No new reports to send.")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ActivityWatch email report generator")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run exactly once. This is the default behavior.",
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
        raise ValueError(f"Invalid timeframes: {invalid_timeframes}. Allowed: {sorted(SUPPORTED_TIMEFRAMES)}")

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
        raise ValueError(f"Invalid timezone: {timezone_name}") from exc

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


def previous_completed_day_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    end = align_start_to_timeframe(now, "daily", tz)
    return end - timedelta(days=1), end


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
        raise RuntimeError("No ActivityWatch window bucket found.")

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


def api_post_json(api_base_url: str, path: str, data: dict[str, Any]) -> Any:
    url = f"{api_base_url}{path}"
    payload = json.dumps(data).encode("utf-8")
    req = Request(
        url,
        data=payload,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as response:
            payload_text = response.read().decode("utf-8")
        return json.loads(payload_text)
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

    if isinstance(raw_value, (list, tuple)):
        parts = [normalize_path_segment(part) for part in raw_value if str(part).strip()]
    elif raw_value is not None:
        parts = [
            normalize_path_segment(part)
            for part in re.split(r"\s*(?:/|>|\\|\|)\s*", str(raw_value))
            if str(part).strip()
        ]
    else:
        parts = []

    return tuple(parts) if parts else (DEFAULT_CATEGORY_NAME,)


def build_report(config: AppConfig, bucket_catalog: BucketCatalog, period: ReportPeriod) -> ReportData:
    canonical_window_events = fetch_canonical_window_events(config, bucket_catalog, period)
    use_event_durations = canonical_window_events is not None
    if canonical_window_events is None:
        window_events = fetch_window_events(config, list(bucket_catalog.window_bucket_ids), period)
        active_intervals = fetch_active_intervals(config, list(bucket_catalog.afk_bucket_ids), period)
    else:
        window_events = canonical_window_events
        active_intervals = []

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
            event_duration_seconds(clipped_start, clipped_end)
            if use_event_durations
            else overlap_seconds_with_intervals(clipped_start, clipped_end, active_intervals)
        )
        if seconds <= 0:
            continue
        app_key = event.app or "Unknown"
        title_key = event.title or "Unknown"
        category_path = event.category_path or resolve_category_path(bucket_catalog, event.bucket_id)
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


def fetch_canonical_window_events(
    config: AppConfig,
    bucket_catalog: BucketCatalog,
    period: ReportPeriod,
) -> list[WindowEvent] | None:
    try:
        categories = load_activitywatch_categories(config.aw_api_base_url)

        events: list[WindowEvent] = []
        for window_bucket_id in bucket_catalog.window_bucket_ids:
            afk_bucket_id = match_afk_bucket(bucket_catalog.afk_bucket_ids, window_bucket_id)
            query = build_canonical_window_query(window_bucket_id, afk_bucket_id, categories)
            raw_result = api_post_json(
                config.aw_api_base_url,
                "/api/0/query/",
                data={
                    "timeperiods": [
                        "/".join(
                            [
                                period.start.astimezone(timezone.utc).isoformat(),
                                period.end.astimezone(timezone.utc).isoformat(),
                            ]
                        )
                    ],
                    "query": query.split("\n"),
                },
            )
            raw_events = unwrap_query_result(raw_result)
            if not isinstance(raw_events, list):
                continue
            for raw in raw_events:
                try:
                    data = raw.get("data", {}) if isinstance(raw, dict) else {}
                    start_value = raw.get("timestamp") if isinstance(raw, dict) else None
                    if not isinstance(start_value, str):
                        continue
                    start = parse_aw_timestamp(start_value, config.timezone)
                    duration = float(raw.get("duration", 0)) if isinstance(raw, dict) else 0.0
                    end = start + timedelta(seconds=duration)
                    events.append(
                        WindowEvent(
                            start=start,
                            end=end,
                            app=str(data.get("app", "")),
                            title=str(data.get("title", "")),
                            bucket_id=window_bucket_id,
                            category_path=extract_category_path(raw, bucket_catalog, window_bucket_id),
                        )
                    )
                except Exception:
                    continue
        events.sort(key=lambda item: item.start)
        return events
    except Exception as exc:
        LOGGER.info("Canonical ActivityWatch categories unavailable, falling back: %s", exc)
        return None


def load_activitywatch_categories(api_base_url: str) -> list[Any]:
    settings = api_get_json(api_base_url, "/api/0/settings")
    if not isinstance(settings, dict):
        return []
    classes = settings.get("classes", [])
    if not isinstance(classes, list):
        return []
    normalized: list[Any] = []
    for raw in classes:
        category = normalize_activitywatch_category(raw)
        if category is not None:
            normalized.append(category)
    return normalized


def normalize_activitywatch_category(raw: Any) -> list[Any] | None:
    if not isinstance(raw, dict):
        return None

    raw_name = raw.get("name")
    if isinstance(raw_name, list):
        name = [normalize_path_segment(part) for part in raw_name if str(part).strip()]
    elif raw_name is not None:
        name = [normalize_path_segment(raw_name)]
    else:
        name = []

    raw_rule = raw.get("rule")
    if not isinstance(raw_rule, dict):
        return None

    rule: dict[str, Any] = {}
    rule_type = str(raw_rule.get("type", "")).strip().lower()
    if rule_type in {"none", "no rule", "no_rule"}:
        rule["type"] = "none"
    else:
        rule["type"] = "regex"
        regex_value = raw_rule.get("regex")
        if regex_value is None:
            return None
        rule["regex"] = str(regex_value)
        if raw_rule.get("ignore_case") is not None:
            rule["ignore_case"] = bool(raw_rule.get("ignore_case"))

    return [name, rule] if name else None


def match_afk_bucket(afk_bucket_ids: tuple[str, ...], window_bucket_id: str) -> str | None:
    suffix = window_bucket_id.split("_", 1)[1] if "_" in window_bucket_id else ""
    if suffix:
        expected = f"aw-watcher-afk_{suffix}"
        for bucket_id in afk_bucket_ids:
            if bucket_id == expected:
                return bucket_id
    return afk_bucket_ids[0] if afk_bucket_ids else None


def build_canonical_window_query(
    window_bucket_id: str,
    afk_bucket_id: str | None,
    categories: list[Any],
) -> str:
    classes_str = json.dumps(categories)
    classes_str = re.sub(r"\\\\", r"\\", classes_str)

    lines = [f'events = flood(query_bucket("{escape_query_string(window_bucket_id)}"));']
    if afk_bucket_id:
        lines.extend(
            [
                f'not_afk = flood(query_bucket("{escape_query_string(afk_bucket_id)}"));',
                'not_afk = filter_keyvals(not_afk, "status", ["not-afk"]);',
                "events = filter_period_intersect(events, not_afk);",
            ]
        )
    if categories:
        lines.append(f"events = categorize(events, {classes_str});")
    lines.append("RETURN = events;")
    return "\n".join(lines)


def escape_query_string(value: str) -> str:
    return value.replace('"', '\\"')


def unwrap_query_result(raw: Any) -> Any:
    if isinstance(raw, dict) and "events" in raw:
        return raw["events"]
    if isinstance(raw, list):
        if len(raw) == 1:
            return raw[0]
        return raw
    return raw


def extract_category_path(raw_event: Any, bucket_catalog: BucketCatalog, bucket_id: str) -> tuple[str, ...]:
    if not isinstance(raw_event, dict):
        return resolve_category_path(bucket_catalog, bucket_id)
    data = raw_event.get("data", {})
    if isinstance(data, dict):
        raw_value: Any = data.get("$category")
        if raw_value is None:
            raw_value = data.get("category")
        if raw_value is not None:
            if isinstance(raw_value, (list, tuple)):
                parts = [normalize_path_segment(part) for part in raw_value if str(part).strip()]
            else:
                parts = [
                    normalize_path_segment(part)
                    for part in re.split(r"\s*(?:/|>|\\|\|)\s*", str(raw_value))
                    if str(part).strip()
                ]
            if parts:
                return tuple(parts)
    return resolve_category_path(bucket_catalog, bucket_id)


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
    return bucket_catalog.category_paths.get(bucket_id, (DEFAULT_CATEGORY_NAME,))


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
    subject = build_email_subject(report.period)
    html = build_html_email(config, report, images)
    return subject, html, images


def build_email_subject(period: ReportPeriod) -> str:
    return f"ActivityWatch {period.timeframe.title()} Report - {period.label}"


def generate_report_images(config: AppConfig, report: ReportData) -> list[tuple[str, bytes]]:
    temp_paths: list[Path] = []
    try:
        category_plot = create_category_plot(report.category_seconds)
        timeline_plot = create_timeline_plot(report, config.top_items_limit, config.week_start_day)

        plots = [
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
        ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=12)
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
    max_depth = max((len(path) for path in category_seconds), default=1)
    height = max(6.6, 5.3 + 0.45 * max(0, max_depth - 2))
    fig, ax = plt.subplots(figsize=(9.8, height))
    if not category_seconds:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=14)
        ax.set_axis_off()
        return fig

    palette = plt.get_cmap("tab20")
    ring_width = 0.78 / max_depth
    inner_hole = 1.0 - ring_width * max_depth

    tree: dict[str, Any] = {}
    totals: dict[tuple[str, ...], float] = defaultdict(float)
    for path, seconds in category_seconds.items():
        node = tree
        for idx, segment in enumerate(path):
            prefix = path[: idx + 1]
            totals[prefix] += seconds
            node = node.setdefault(segment, {})

    root_names = sorted(tree.keys(), key=lambda name: totals.get((name,), 0.0), reverse=True)
    root_color_index = {name: idx for idx, name in enumerate(root_names)}

    def node_color(path: tuple[str, ...]) -> tuple[float, float, float, float]:
        root_index = root_color_index.get(path[0], 0)
        return palette((root_index * 3 + len(path) - 1) % 20)

    def text_color(rgba: tuple[float, float, float, float]) -> str:
        r, g, b, _ = rgba
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        return "white" if luminance < 0.6 else "#1f2937"

    def wrap_label(label: str, width: int) -> str:
        wrapped = textwrap.wrap(label, width=width, break_long_words=False, break_on_hyphens=False) or [label]
        return "\n".join(wrapped[:2])

    outside_labels: list[dict[str, Any]] = []

    def label_requires_outside(depth: int, angle_span: float) -> bool:
        if depth >= 1:
            return True
        return angle_span < 28.0

    def draw_node(
        node: dict[str, Any],
        prefix: tuple[str, ...],
        start_angle: float,
        span: float,
        depth: int,
    ) -> None:
        if not node:
            return
        total = totals[prefix] if prefix else sum(totals[(child_name,)] for child_name in node.keys())
        if total <= 0:
            return

        ordered_children = sorted(
            node.items(),
            key=lambda item: totals[prefix + (item[0],)],
            reverse=True,
        )
        current_angle = start_angle
        for child_name, child_node in ordered_children:
            child_path = prefix + (child_name,)
            child_total = totals[child_path]
            if child_total <= 0:
                continue
            child_span = span * (child_total / total)
            theta1 = current_angle
            theta2 = current_angle + child_span
            radius = inner_hole + (depth + 1) * ring_width
            wedge = Wedge(
                (0.0, 0.0),
                radius,
                theta1,
                theta2,
                width=ring_width,
                facecolor=node_color(child_path),
                edgecolor="white",
                linewidth=1.2,
            )
            ax.add_patch(wedge)

            angle = math.radians((theta1 + theta2) / 2.0)
            angle_span = theta2 - theta1
            label_radius = inner_hole + (depth + 0.5) * ring_width
            label_area = angle_span * ring_width
            label = wrap_label(child_name, 14 if depth == 0 else 12)
            if label_area >= 0.16 and not label_requires_outside(depth, angle_span):
                ax.text(
                    math.cos(angle) * label_radius,
                    math.sin(angle) * label_radius,
                    label,
                    ha="center",
                    va="center",
                    fontsize=max(7.8, 10.2 - depth * 0.8),
                    fontweight="bold" if depth == 0 else "normal",
                    color=text_color(wedge.get_facecolor()),
                    clip_on=True,
                )
            else:
                side = 1 if math.cos(angle) >= 0 else -1
                outside_labels.append(
                    {
                        "side": side,
                        "angle": angle,
                        "y_target": max(-1.15, min(1.15, math.sin(angle) * 1.08)),
                        "anchor": (
                            math.cos(angle) * (radius - ring_width * 0.03),
                            math.sin(angle) * (radius - ring_width * 0.03),
                        ),
                        "label": label,
                        "color": text_color(wedge.get_facecolor()),
                        "depth": depth,
                    }
                )

            draw_node(child_node, child_path, theta1, child_span, depth + 1)
            current_angle += child_span

    def distribute_label_positions(items: list[dict[str, Any]], low: float, high: float) -> list[float]:
        if not items:
            return []
        ordered = sorted(items, key=lambda item: (item["y_target"], item["angle"]))
        count = len(ordered)
        if count == 1:
            return [min(high, max(low, ordered[0]["y_target"]))]

        min_gap = 0.17 if count <= 6 else 0.13
        required_span = min_gap * (count - 1)
        span = high - low
        if required_span > span:
            extra = (required_span - span) / 2.0
            low -= extra
            high += extra

        positions = [ordered[0]["y_target"]]
        for item in ordered[1:]:
            positions.append(max(item["y_target"], positions[-1] + min_gap))

        shift = 0.0
        if positions[-1] > high:
            shift = high - positions[-1]
        if positions[0] + shift < low:
            shift = low - positions[0]
        positions = [y + shift for y in positions]
        return positions

    draw_node(tree, tuple(), 90.0, 360.0, 0)
    label_groups = {
        1: [item for item in outside_labels if item["side"] > 0],
        -1: [item for item in outside_labels if item["side"] < 0],
    }
    band_ranges = {1: (-1.15, 1.15), -1: (-1.15, 1.15)}
    text_xs = {1: 1.7, -1: -1.7}
    for side, items in label_groups.items():
        if not items:
            continue
        positions = distribute_label_positions(items, *band_ranges[side])
        ordered = sorted(items, key=lambda item: (item["y_target"], item["angle"]))
        count = len(ordered)
        for idx, (item, y) in enumerate(zip(ordered, positions)):
            anchor_x, anchor_y = item["anchor"]
            elbow_step = 0.08 if count <= 5 else 0.11
            elbow_x = side * (1.03 + elbow_step * idx)
            ax.plot(
                [anchor_x, elbow_x, text_xs[side]],
                [anchor_y, y, y],
                color=item["color"],
                lw=0.95,
                solid_capstyle="round",
                zorder=2,
            )
            ax.text(
                text_xs[side],
                y,
                item["label"],
                ha="left" if side > 0 else "right",
                va="center",
                fontsize=max(7.2, 9.8 - item["depth"] * 0.5),
                color="#1f2937",
                bbox=dict(boxstyle="round,pad=0.14", facecolor="white", edgecolor="none", alpha=0.9),
            )

    ax.set_aspect("equal")
    ax.set_title("Category Sunburst", loc="left", fontsize=15, pad=12)

    ax.set_xlim(-1.88, 1.88)
    ax.set_ylim(-1.28, 1.28)
    ax.set_axis_off()
    fig.subplots_adjust(left=0.02, right=0.98, top=0.94, bottom=0.04)
    return fig


def create_timeline_plot(report: ReportData, top_items_limit: int, week_start_day: int = 0) -> Figure:
    fig, ax = plt.subplots(figsize=(7.4, 5.8))
    if not report.timeline_seconds:
        ax.text(0.5, 0.5, "No data", ha="center", va="center", fontsize=12)
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
    ax.set_title("Timeline (bar chart)", loc="left", fontsize=15, pad=12)
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
    category_hierarchy_html = build_category_hierarchy_html(report.category_seconds)
    top_titles_html = build_bar_list_html(report.title_seconds, config.top_items_limit)
    top_apps_html = build_bar_list_html(report.app_seconds, config.top_items_limit)

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
            margin: 0;
            padding: 0;
          }}
          .wrap {{
            max-width: 820px;
            margin: 0 auto;
            padding: 18px 20px 28px;
          }}
          .header {{
            margin-bottom: 12px;
          }}
          .header h1 {{
            margin: 0;
            font-size: 30px;
            font-weight: 700;
          }}
          .summary {{
            display: block;
            margin-top: 10px;
            color: #5b6574;
            font-size: 13px;
          }}
          .summary-item {{
            margin-top: 2px;
          }}
          .card {{
            background: #fff;
            border-top: 1px solid #e6e8ee;
            padding: 14px 0 14px;
            box-sizing: border-box;
            overflow: hidden;
          }}
          .card h2 {{
            margin: 0 0 10px;
            font-size: 21px;
            font-weight: 700;
            color: #243041;
          }}
          .report-sections {{
            display: block;
          }}
          .report-sections .card img {{
            margin-top: 0;
          }}
          .plain-list {{
            display: block;
          }}
          .plain-line {{
            display: block;
            font-size: 13px;
            line-height: 1.35;
            color: #243041;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            padding: 4px 0;
          }}
          .plain-line em {{
            font-style: italic;
          }}
          .metric-list {{
            display: block;
          }}
          .metric-item {{
            --bar-color: #87cefa;
            display: block;
            width: 100%;
            background: rgba(103, 208, 255, 0.12);
            border-left: 8px solid var(--bar-color);
            border-radius: 6px;
            padding: 7px 10px 7px 10px;
            color: #20252d;
            box-sizing: border-box;
            margin-top: 8px;
          }}
          .metric-item:first-child {{
            margin-top: 0;
          }}
          .metric-label {{
            font-size: 13px;
            line-height: 1.1;
            word-break: break-word;
          }}
          .metric-duration {{
            font-size: 12px;
            margin-top: 3px;
          }}
          .metric-percent {{
            font-size: 12px;
            margin-top: 1px;
            color: rgba(32, 37, 45, 0.82);
          }}
          .muted {{
            color: #6b7280;
          }}
          .category-list {{
            display: block;
          }}
          .category-root {{
            display: block;
            padding: 8px 0 4px;
            border-top: 1px solid #eef1f5;
          }}
          .category-root:first-child {{
            border-top: 0;
            padding-top: 0;
          }}
          .category-sublist {{
            display: block;
            margin: 0 0 8px 18px;
            padding-left: 10px;
            border-left: 2px solid #eef1f5;
          }}
          .category-entry {{
            display: block;
            padding: 4px 0;
          }}
          .category-line {{
            display: block;
            font-size: 13px;
            line-height: 1.35;
            color: #243041;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }}
          img {{
            max-width: 100%;
            height: auto;
            display: block;
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="header">
            <h1>ActivityWatch {escape_html(report.period.timeframe.title())} Report</h1>
            <div class="summary">
              <div class="summary-item"><strong>Period:</strong> {escape_html(report.period.label)}</div>
              <div class="summary-item"><strong>Total time:</strong> {escape_html(total_time)}</div>
              <div class="summary-item"><strong>Events found:</strong> {report.events_found}</div>
            </div>
          </div>

          <div class="report-sections">
            <section class="card">
              <h2>Categories</h2>
              {category_hierarchy_html}
            </section>
            <section class="card">
              <h2>Category Sunburst</h2>
              <img src="cid:category" alt="Category Sunburst" />
            </section>
            <section class="card">
              <h2>Timeline (bar chart)</h2>
              <img src="cid:timeline" alt="Timeline" />
            </section>
            <section class="card">
              <h2>Top Window Titles</h2>
              {top_titles_html}
            </section>
            <section class="card">
              <h2>Top Applications</h2>
              {top_apps_html}
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
        return "<p class='muted'>No data</p>"
    total_seconds = sum(values.values()) or 1.0
    html_rows = ['<div class="plain-list">']
    for label, seconds in rows:
        rendered_label = item_formatter(label) if item_formatter else label
        percent = seconds / total_seconds * 100.0
        html_rows.append(
            f'<div class="plain-line">{escape_html(str(rendered_label))} - '
            f'{escape_html(format_duration_compact(seconds))} - '
            f'<em>{percent:.1f}% total</em></div>'
        )
    html_rows.append("</div>")
    return "".join(html_rows)


def build_category_hierarchy_html(category_seconds: dict[tuple[str, ...], float]) -> str:
    if not category_seconds:
        return "<p class='muted'>No data</p>"

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

    root_items = sorted(tree.keys(), key=lambda name: totals.get((name,), 0.0), reverse=True)
    html: list[str] = ['<div class="category-list">']

    def render_node(node: dict[str, Any], prefix: tuple[str, ...], parent_seconds: float) -> str:
        entries: list[str] = []
        for name in sorted(node.keys(), key=lambda key: totals.get(prefix + (key,), 0.0), reverse=True):
            current_path = prefix + (name,)
            seconds = totals.get(current_path, 0.0)
            percent_total = seconds / total_seconds * 100
            percent_parent = seconds / parent_seconds * 100 if parent_seconds else 0.0
            children = node[name]
            child_html = render_node(children, current_path, seconds) if children else ""
            entries.append(
                '<div class="category-entry">'
                f'<div class="category-line" style="padding-left: {len(prefix) * 16}px;">'
                f'{escape_html(name)} - {escape_html(format_duration_compact(seconds))} - '
                f'<em>{percent_total:.1f}% total - {percent_parent:.1f}% of parent category</em>'
                "</div>"
                f"{child_html}"
                "</div>"
            )
        return "".join(entries)

    for root_name in root_items:
        root_path = (root_name,)
        root_seconds = totals.get(root_path, 0.0)
        root_children = tree[root_name]
        root_total_percent = root_seconds / total_seconds * 100
        html.append(
            '<div class="category-root">'
            f'<div class="category-line">{escape_html(root_name)} - {escape_html(format_duration_compact(root_seconds))} - '
            f'<em>{root_total_percent:.1f}% total</em></div>'
            "</div>"
        )
        if root_children:
            html.append(f'<div class="category-sublist">{render_node(root_children, root_path, root_seconds)}</div>')

    html.append("</div>")
    return "".join(html)


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
