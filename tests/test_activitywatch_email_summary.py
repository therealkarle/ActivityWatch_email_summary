from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "activitywatch_email_summary.py"

spec = importlib.util.spec_from_file_location("activitywatch_email_summary", MODULE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Could not load module from {MODULE_PATH}")

aw = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = aw
spec.loader.exec_module(aw)


class ActivityWatchEmailSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tz = ZoneInfo("Europe/Berlin")

    def test_week_start_is_configurable(self) -> None:
        value = datetime(2026, 6, 24, 15, 30, tzinfo=self.tz)
        week_start = aw.parse_week_start_day("sun")

        aligned = aw.align_start_to_timeframe(value, "weekly", self.tz, week_start)

        self.assertEqual(aligned, datetime(2026, 6, 21, 0, 0, tzinfo=self.tz))

    def test_monthly_metadata_and_timeline_buckets(self) -> None:
        value = datetime(2026, 6, 15, 10, 0, tzinfo=self.tz)
        key, label = aw.build_period_metadata(value, "monthly", self.tz)
        period = aw.ReportPeriod(
            timeframe="monthly",
            start=value,
            end=value + timedelta(days=1),
            label=label,
            key=key,
        )

        self.assertEqual((key, label), ("2026-06", "2026-06"))
        self.assertEqual(aw.get_timeline_bucket(period, value, self.tz), "15")
        buckets = aw.ordered_timeline_buckets("monthly")
        self.assertEqual(buckets[0], "01")
        self.assertEqual(buckets[-1], "31")
        self.assertEqual(len(buckets), 31)
        self.assertEqual(aw.ordered_timeline_buckets("weekly", aw.parse_week_start_day("sun"))[0], "Sun")

    def test_email_subject_includes_timeframe_and_label(self) -> None:
        period = aw.ReportPeriod(
            timeframe="daily",
            start=datetime(2026, 6, 25, 0, 0, tzinfo=self.tz),
            end=datetime(2026, 6, 26, 0, 0, tzinfo=self.tz),
            label="2026-06-25",
            key="2026-06-25",
        )

        subject = aw.build_email_subject(period)

        self.assertEqual(subject, "ActivityWatch Daily Report - 2026-06-25")

    def test_first_run_date_is_initialized_on_first_start(self) -> None:
        sent_log = {"reports": {}}
        now = datetime(2026, 6, 25, 14, 45, tzinfo=self.tz)

        first_run_date, updated = aw.ensure_first_run_date(sent_log, now, self.tz)

        self.assertTrue(updated)
        self.assertEqual(first_run_date.isoformat(), "2026-06-25")
        self.assertEqual(sent_log["first_run_date"], "2026-06-25")

    def test_previous_completed_day_window_uses_yesterday(self) -> None:
        now = datetime(2026, 6, 26, 14, 45, tzinfo=self.tz)

        start, end = aw.previous_completed_day_window(now, self.tz)

        self.assertEqual(start, datetime(2026, 6, 25, 0, 0, tzinfo=self.tz))
        self.assertEqual(end, datetime(2026, 6, 26, 0, 0, tzinfo=self.tz))

    def test_enumerate_periods_keeps_calendar_key_when_clipped(self) -> None:
        start = datetime(2026, 6, 25, 0, 0, tzinfo=self.tz)
        end = datetime(2026, 6, 25, 18, 0, tzinfo=self.tz)

        periods = aw.enumerate_periods("weekly", start, end, self.tz, aw.parse_week_start_day("mon"))

        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0].start, start)
        self.assertEqual(periods[0].key, "2026-06-22")
        self.assertEqual(periods[0].label, "2026-06-22")

    def test_extract_category_path_prefers_query_category(self) -> None:
        bucket_catalog = aw.BucketCatalog(window_bucket_ids=(), afk_bucket_ids=(), category_paths={})

        path = aw.extract_category_path(
            {"data": {"$category": ["Work", "Mail"]}},
            bucket_catalog,
            "aw-watcher-window_FlorianPC",
        )

        self.assertEqual(path, ("Work", "Mail"))

    def test_build_report_uses_canonical_categories_when_available(self) -> None:
        config = aw.AppConfig(
            enabled_timeframes=("daily",),
            top_items_limit=5,
            lookback_days=30,
            week_start_day=0,
            aw_api_base_url="http://localhost:5600",
            timezone=self.tz,
            smtp_settings=aw.SMTPSettings(
                server="smtp.example.com",
                port=587,
                sender_email="from@example.com",
                recipient_email="to@example.com",
                username="user",
                password="pass",
            ),
        )
        period_start = datetime(2026, 6, 25, 0, 0, tzinfo=self.tz)
        period = aw.ReportPeriod(
            timeframe="daily",
            start=period_start,
            end=period_start + timedelta(hours=1),
            label="2026-06-25",
            key="2026-06-25",
        )
        bucket_catalog = aw.BucketCatalog(
            window_bucket_ids=("aw-watcher-window_FlorianPC",),
            afk_bucket_ids=("aw-watcher-afk_FlorianPC",),
            category_paths={},
        )
        canonical_events = [
            aw.WindowEvent(
                start=period_start,
                end=period_start + timedelta(hours=1),
                app="Code",
                title="main.py",
                bucket_id="aw-watcher-window_FlorianPC",
                category_path=("Work", "Dev"),
            )
        ]

        with mock.patch.object(aw, "fetch_canonical_window_events", return_value=canonical_events), mock.patch.object(
            aw, "fetch_window_events"
        ) as raw_fetch, mock.patch.object(aw, "fetch_active_intervals") as active_fetch:
            report = aw.build_report(config, bucket_catalog, period)

        raw_fetch.assert_not_called()
        active_fetch.assert_not_called()
        self.assertEqual(report.category_seconds, {("Work", "Dev"): 3600.0})
        self.assertEqual(report.app_seconds["Code"], 3600.0)
        self.assertEqual(report.title_seconds["main.py"], 3600.0)

    def test_build_report_falls_back_to_uncategorized(self) -> None:
        config = aw.AppConfig(
            enabled_timeframes=("daily",),
            top_items_limit=5,
            lookback_days=30,
            week_start_day=0,
            aw_api_base_url="http://localhost:5600",
            timezone=self.tz,
            smtp_settings=aw.SMTPSettings(
                server="smtp.example.com",
                port=587,
                sender_email="from@example.com",
                recipient_email="to@example.com",
                username="user",
                password="pass",
            ),
        )
        period_start = datetime(2026, 6, 25, 0, 0, tzinfo=self.tz)
        period = aw.ReportPeriod(
            timeframe="daily",
            start=period_start,
            end=period_start + timedelta(hours=1),
            label="2026-06-25",
            key="2026-06-25",
        )
        bucket_catalog = aw.BucketCatalog(
            window_bucket_ids=("aw-watcher-window_FlorianPC",),
            afk_bucket_ids=("aw-watcher-afk_FlorianPC",),
            category_paths={},
        )
        raw_events = [
            aw.WindowEvent(
                start=period_start,
                end=period_start + timedelta(hours=1),
                app="Code",
                title="main.py",
                bucket_id="aw-watcher-window_FlorianPC",
            )
        ]

        with mock.patch.object(aw, "fetch_canonical_window_events", return_value=None), mock.patch.object(
            aw, "fetch_window_events", return_value=raw_events
        ), mock.patch.object(aw, "fetch_active_intervals", return_value=[(period_start, period_start + timedelta(hours=1))]):
            report = aw.build_report(config, bucket_catalog, period)

        self.assertEqual(report.category_seconds, {("Uncategorized",): 3600.0})


if __name__ == "__main__":
    unittest.main()
