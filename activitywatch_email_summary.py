from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import smtplib
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email import encoders
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


CONFIG_FILE = Path("config.json")
DEFAULT_CONFIG_EXAMPLE_FILE = Path("config.example.json")
SENT_LOG_FILE = Path.home() / ".aw_sent_log.json"
DEFAULT_API_BASE_URL = "http://localhost:5600"
DEFAULT_TIMEZONE = "Europe/Berlin"
DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_TOP_ITEMS_LIMIT = 5
SUPPORTED_TIMEFRAMES = {"daily", "weekly", "yearly"}
WINDOW_BUCKET_HINTS = ("aw-watcher-window", "window")
AFK_BUCKET_HINTS = ("aw-watcher-afk", "afk")
WEEKDAY_BUCKETS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_BUCKETS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass(frozen=True)
class CategoryRule:
    path: tuple[str, ...]
    app_patterns: tuple[re.Pattern[str], ...] = ()
    title_patterns: tuple[re.Pattern[str], ...] = ()
    bucket_patterns: tuple[re.Pattern[str], ...] = ()


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
    aw_api_base_url: str
    timezone: ZoneInfo
    default_category_path: tuple[str, ...]
    category_rules: tuple[CategoryRule, ...]
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


def main() -> int:
    args = parse_args()
    try:
        config = load_config(CONFIG_FILE)
    except Exception as exc:
        print(f"Fehler beim Laden der Konfiguration: {exc}", file=sys.stderr)
        if not CONFIG_FILE.exists():
            print(
                f"Lege {CONFIG_FILE} an, indem du {DEFAULT_CONFIG_EXAMPLE_FILE} kopierst und ausfüllst.",
                file=sys.stderr,
            )
        return 1

    if args.once is False:
        # Default behaviour: one boot-time run. The flag exists mainly for testability.
        pass

    time.sleep(15)

    try:
        sent_log = load_sent_log(SENT_LOG_FILE)
    except Exception as exc:
        print(f"Fehler beim Laden des lokalen Sent-Logs: {exc}", file=sys.stderr)
        return 1

    try:
        bucket_index = discover_buckets(config.aw_api_base_url)
    except Exception as exc:
        print(f"ActivityWatch-API nicht erreichbar oder nicht lesbar: {exc}", file=sys.stderr)
        return 1

    processed_any = False
    now = datetime.now(config.timezone)
    lookback_start = now - timedelta(days=config.lookback_days)

    for timeframe in config.enabled_timeframes:
        periods = enumerate_periods(timeframe, lookback_start, now, config.timezone)
        for period in periods:
            if is_period_logged(sent_log, period):
                continue

            try:
                report = build_report(config, bucket_index, period)
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
                    print(f"Sent-Log konnte nach Fehler nicht geschrieben werden: {log_exc}", file=sys.stderr)
                print(
                    f"Fehler bei {period.timeframe} {period.label}: {exc}",
                    file=sys.stderr,
                )
                print(traceback.format_exc(), file=sys.stderr)
                continue

    if not processed_any:
        print("Keine neuen Berichte zu versenden.")

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

    aw_api_base_url = str(raw.get("aw_api_base_url", DEFAULT_API_BASE_URL)).rstrip("/")
    timezone_name = str(raw.get("timezone", DEFAULT_TIMEZONE))
    try:
        tz = ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"Ungültige Zeitzone: {timezone_name}") from exc

    default_category_path = tuple(
        normalize_path_segment(part) for part in raw.get("default_category_path", ["Uncategorized"])
    )
    if not default_category_path:
        default_category_path = ("Uncategorized",)

    category_rules_raw = raw.get("category_rules", [])
    if not isinstance(category_rules_raw, list):
        raise ValueError("category_rules muss eine Liste sein.")
    category_rules = tuple(parse_category_rules(category_rules_raw))
    smtp_settings = parse_smtp_settings(raw.get("smtp_settings", {}))

    return AppConfig(
        enabled_timeframes=enabled_timeframes,
        top_items_limit=top_items_limit,
        lookback_days=lookback_days,
        aw_api_base_url=aw_api_base_url,
        timezone=tz,
        default_category_path=default_category_path,
        category_rules=category_rules,
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


def parse_category_rules(raw_rules: list[dict[str, Any]]) -> Iterable[CategoryRule]:
    for raw_rule in raw_rules:
        path_raw = raw_rule.get("path")
        if not path_raw:
            raise ValueError("Jede category_rules-Regel braucht einen path.")
        path = tuple(normalize_path_segment(part) for part in path_raw)
        if not path:
            raise ValueError("category_rules.path darf nicht leer sein.")

        match = raw_rule.get("match", {})
        app_patterns = tuple(compile_patterns(match.get("app_patterns", [])))
        title_patterns = tuple(compile_patterns(match.get("title_patterns", [])))
        bucket_patterns = tuple(compile_patterns(match.get("bucket_patterns", [])))
        yield CategoryRule(
            path=path,
            app_patterns=app_patterns,
            title_patterns=title_patterns,
            bucket_patterns=bucket_patterns,
        )


def compile_patterns(patterns: list[str]) -> Iterable[re.Pattern[str]]:
    for pattern in patterns:
        yield re.compile(pattern)


def normalize_timeframe(value: str) -> str:
    return str(value).strip().lower()


def normalize_path_segment(value: Any) -> str:
    text = str(value).strip()
    return text if text else "Uncategorized"


def load_sent_log(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"reports": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"reports": {}}


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


def enumerate_periods(timeframe: str, start: datetime, end: datetime, tz: ZoneInfo) -> list[ReportPeriod]:
    timeframe = normalize_timeframe(timeframe)
    periods: list[ReportPeriod] = []
    current_start = align_start_to_timeframe(start, timeframe, tz)
    while current_start < end:
        current_end = add_timeframe(current_start, timeframe, tz)
        if current_end <= start:
            current_start = current_end
            continue
        clipped_start = max(current_start, start)
        clipped_end = min(current_end, end)
        key, label = build_period_metadata(clipped_start, timeframe, tz)
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


def align_start_to_timeframe(value: datetime, timeframe: str, tz: ZoneInfo) -> datetime:
    localized = value.astimezone(tz)
    if timeframe == "daily":
        return localized.replace(hour=0, minute=0, second=0, microsecond=0)
    if timeframe == "weekly":
        start_of_day = localized.replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day - timedelta(days=start_of_day.weekday())
    if timeframe == "yearly":
        return localized.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def add_timeframe(start: datetime, timeframe: str, tz: ZoneInfo) -> datetime:
    if timeframe == "daily":
        return start + timedelta(days=1)
    if timeframe == "weekly":
        return start + timedelta(days=7)
    if timeframe == "yearly":
        return start.replace(year=start.year + 1)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def build_period_metadata(start: datetime, timeframe: str, tz: ZoneInfo) -> tuple[str, str]:
    localized = start.astimezone(tz)
    if timeframe == "daily":
        return localized.date().isoformat(), localized.strftime("%Y-%m-%d")
    if timeframe == "weekly":
        iso_year, iso_week, _ = localized.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        return key, key
    if timeframe == "yearly":
        return str(localized.year), str(localized.year)
    raise ValueError(f"Unsupported timeframe: {timeframe}")


def discover_buckets(api_base_url: str) -> dict[str, list[str]]:
    buckets = api_get_json(api_base_url, "/api/0/buckets")
    window_bucket_ids = []
    afk_bucket_ids = []
    for bucket_id in buckets.keys():
        if any(hint in bucket_id.lower() for hint in WINDOW_BUCKET_HINTS):
            window_bucket_ids.append(bucket_id)
        if any(hint in bucket_id.lower() for hint in AFK_BUCKET_HINTS):
            afk_bucket_ids.append(bucket_id)

    if not window_bucket_ids:
        raise RuntimeError("Kein ActivityWatch-Window-Bucket gefunden.")

    return {"window": sorted(window_bucket_ids), "afk": sorted(afk_bucket_ids)}


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


def build_report(config: AppConfig, bucket_index: dict[str, list[str]], period: ReportPeriod) -> ReportData:
    window_events = fetch_window_events(config, bucket_index["window"], period)
    active_intervals = fetch_active_intervals(config, bucket_index.get("afk", []), period)

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
        category_path = resolve_category_path(config, event)
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


def resolve_category_path(config: AppConfig, event: WindowEvent) -> tuple[str, ...]:
    for rule in config.category_rules:
        if rule.bucket_patterns and not any(pattern.search(event.bucket_id) for pattern in rule.bucket_patterns):
            continue
        if rule.app_patterns and not any(pattern.search(event.app or "") for pattern in rule.app_patterns):
            continue
        if rule.title_patterns and not any(pattern.search(event.title or "") for pattern in rule.title_patterns):
            continue
        return rule.path
    return config.default_category_path


def category_root(path: tuple[str, ...]) -> str:
    return path[0] if path else "Uncategorized"


def get_timeline_bucket(period: ReportPeriod, moment: datetime, tz: ZoneInfo) -> str:
    localized = moment.astimezone(tz)
    if period.timeframe == "daily":
        return f"{localized.hour:02d}:00"
    if period.timeframe == "weekly":
        return WEEKDAY_BUCKETS[localized.weekday()]
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
        timeline_plot = create_timeline_plot(report, config.top_items_limit)
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


def create_horizontal_bar_chart(items: list[tuple[str, float]], title: str) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(10, max(3, 0.45 * max(1, len(items)) + 1)))
    if not items:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", fontsize=12)
        ax.set_axis_off()
        return fig

    labels = [label[:60] for label, _ in items]
    values = [seconds_to_hours(seconds) for _, seconds in items]
    y_pos = list(range(len(items)))
    ax.barh(y_pos, values, color="#2f6fed")
    ax.set_yticks(y_pos, labels)
    ax.invert_yaxis()
    ax.set_xlabel("Stunden")
    ax.set_title(title)
    for idx, value in enumerate(values):
        ax.text(value + max(values) * 0.01 + 0.02, idx, f"{value:.2f}", va="center", fontsize=9)
    fig.tight_layout()
    return fig


def create_category_plot(category_seconds: dict[tuple[str, ...], float]) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(9, 9))
    if not category_seconds:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", fontsize=14)
        ax.set_axis_off()
        return fig

    leaf_items = sorted(category_seconds.items(), key=lambda item: item[1], reverse=True)
    inner_totals: dict[str, float] = defaultdict(float)
    outer_labels: list[str] = []
    outer_sizes: list[float] = []
    outer_colors: list[str] = []
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
    )
    ax.pie(
        outer_sizes,
        labels=outer_labels,
        radius=1.0 - 0.28,
        labeldistance=0.78,
        wedgeprops=dict(width=0.28, edgecolor="white"),
        startangle=90,
        colors=outer_colors,
        textprops={"fontsize": 8},
    )
    ax.set_title("Category Distribution")
    return fig


def create_timeline_plot(report: ReportData, top_items_limit: int) -> plt.Figure:
    fig, ax = plt.subplots(figsize=(12, 5))
    if not report.timeline_seconds:
        ax.text(0.5, 0.5, "Keine Daten", ha="center", va="center", fontsize=12)
        ax.set_axis_off()
        return fig

    buckets = ordered_timeline_buckets(report.period.timeframe)
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
            heights.append(seconds_to_hours(height))
        ax.bar(buckets, heights, bottom=bottom, color=cmap(idx % 20), label=category)
        bottom = [b + h for b, h in zip(bottom, heights)]

    ax.set_ylabel("Stunden")
    ax.set_title("Timeline by Category")
    ax.legend(loc="upper left", bbox_to_anchor=(1.02, 1.0))
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    return fig


def ordered_timeline_buckets(timeframe: str) -> list[str]:
    if timeframe == "daily":
        return [f"{hour:02d}:00" for hour in range(24)]
    if timeframe == "weekly":
        return WEEKDAY_BUCKETS
    if timeframe == "yearly":
        return MONTH_BUCKETS
    return []


def seconds_to_hours(seconds: float) -> float:
    return seconds / 3600.0


def build_html_email(config: AppConfig, report: ReportData, images: list[tuple[str, bytes]]) -> str:
    total_time = format_seconds(report.total_seconds)
    category_tree_html = build_category_tree_html(report.category_seconds)
    top_apps_html = build_simple_table(report.app_seconds, config.top_items_limit, "Application")
    top_titles_html = build_simple_table(report.title_seconds, config.top_items_limit, "Window Title")

    image_html = []
    for cid, _ in images:
        title = {
            "top-apps": "Top Applications",
            "top-titles": "Top Window Titles",
            "category": "Category Distribution",
            "timeline": "Timeline",
        }.get(cid, cid)
        image_html.append(
            f"""
            <section class="card">
              <h2>{escape_html(title)}</h2>
              <img src="cid:{cid}" alt="{escape_html(title)}" />
            </section>
            """
        )

    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        <style>
          body {{
            font-family: Arial, sans-serif;
            color: #1f2937;
            line-height: 1.5;
          }}
          .wrap {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 20px;
          }}
          .summary {{
            background: #f3f4f6;
            border-radius: 12px;
            padding: 16px;
            margin-bottom: 20px;
          }}
          .card {{
            margin: 24px 0;
            padding: 16px;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            background: #fff;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            margin: 12px 0 0;
          }}
          th, td {{
            border: 1px solid #e5e7eb;
            padding: 8px 10px;
            text-align: left;
            vertical-align: top;
          }}
          th {{
            background: #f9fafb;
          }}
          ul {{
            margin: 8px 0 8px 22px;
          }}
          img {{
            max-width: 100%;
            height: auto;
            display: block;
            margin-top: 12px;
          }}
          .muted {{
            color: #6b7280;
          }}
          .columns {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
          }}
        </style>
      </head>
      <body>
        <div class="wrap">
          <h1>ActivityWatch {escape_html(report.period.timeframe.title())} Report</h1>
          <div class="summary">
            <div><strong>Zeitraum:</strong> {escape_html(report.period.label)}</div>
            <div><strong>Gesamtzeit:</strong> {escape_html(total_time)}</div>
            <div><strong>Gefundene Events:</strong> {report.events_found}</div>
          </div>

          <section class="card">
            <h2>Hierarchische Kategorien</h2>
            {category_tree_html}
          </section>

          <div class="columns">
            <section class="card">
              <h2>Top Applications</h2>
              {top_apps_html}
            </section>
            <section class="card">
              <h2>Top Window Titles</h2>
              {top_titles_html}
            </section>
          </div>

          {''.join(image_html)}
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


def build_simple_table(values: dict[str, float], limit: int, label_name: str) -> str:
    rows = top_n_items(values, limit)
    if not rows:
        return "<p class='muted'>Keine Daten</p>"
    total = sum(values.values()) or 1.0
    html_rows = []
    for label, seconds in rows:
        html_rows.append(
            f"<tr><td>{escape_html(label)}</td><td>{format_seconds(seconds)}</td><td>{seconds / total * 100:.1f}%</td></tr>"
        )
    return f"""
    <table>
      <thead>
        <tr><th>{escape_html(label_name)}</th><th>Time</th><th>%</th></tr>
      </thead>
      <tbody>
        {''.join(html_rows)}
      </tbody>
    </table>
    """


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

    def render_node(node: dict[str, Any], prefix: tuple[str, ...]) -> str:
        items = []
        for name in sorted(node.keys()):
            current_path = prefix + (name,)
            seconds = totals.get(current_path, 0.0)
            percent = seconds / total_seconds * 100
            children = node[name]
            children_html = render_node(children, current_path) if children else ""
            items.append(
                f"<li><strong>{escape_html(name)}</strong> - {format_seconds(seconds)} ({percent:.1f}%)"
                + (f"{children_html}" if children_html else "")
                + "</li>"
            )
        return f"<ul>{''.join(items)}</ul>" if items else ""

    return render_node(tree, ())


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
