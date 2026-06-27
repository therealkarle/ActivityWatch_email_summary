from __future__ import annotations

import math
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

    def build_config(self, top_items_limit: int = 5) -> aw.AppConfig:
        return aw.AppConfig(
            enabled_timeframes=("daily",),
            top_items_limit=top_items_limit,
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

    def build_report(self) -> aw.ReportData:
        period_start = datetime(2026, 6, 25, 0, 0, tzinfo=self.tz)
        period = aw.ReportPeriod(
            timeframe="daily",
            start=period_start,
            end=period_start + timedelta(hours=1),
            label="2026-06-25",
            key="2026-06-25",
        )
        return aw.ReportData(
            period=period,
            total_seconds=7200.0,
            app_seconds={"Code": 3600.0, "Firefox": 1800.0, "Explorer": 1800.0},
            title_seconds={
                "main.py": 3600.0,
                "ActivityWatch — Mozilla Firefox": 1800.0,
                "docs": 1800.0,
            },
            category_seconds={
                ("Work", "Dev"): 3600.0,
                ("Work", "Mail"): 1800.0,
                ("Comms", "Email"): 1800.0,
            },
            timeline_seconds={"09:00": {"Work": 60.0}},
            events_found=7,
        )

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
        config = self.build_config()
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

    def test_category_hierarchy_includes_root_and_child_percentages(self) -> None:
        html = aw.build_category_hierarchy_html(
            {
                ("Work", "Dev"): 3600.0,
                ("Work", "Mail"): 1800.0,
                ("Comms", "Email"): 1800.0,
            }
        )

        self.assertIn("Work", html)
        self.assertNotIn("•", html)
        self.assertNotIn("⊞", html)
        self.assertIn("Work - 1h 30m 0s - <em>75.0% total</em>", html)
        self.assertIn("Dev - 1h 0m 0s - <em>50.0% total - 66.7% of parent category</em>", html)
        self.assertIn("Mail - 30m 0s - <em>25.0% total - 33.3% of parent category</em>", html)
        self.assertIn("Comms - 30m 0s - <em>25.0% total</em>", html)

    def test_top_lists_use_plain_text_rows(self) -> None:
        html = aw.build_bar_list_html(
            {"ActivityWatch — Mozilla Firefox": 3600.0, "Chrono Analyzer": 1800.0},
            5,
        )

        self.assertIn("ActivityWatch — Mozilla Firefox - 1h 0m 0s - <em>66.7% total</em>", html)
        self.assertIn("Chrono Analyzer - 30m 0s - <em>33.3% total</em>", html)
        self.assertNotIn("metric-item", html)
        self.assertNotIn("metric-percent", html)

    def test_email_layout_is_stacked_and_text_first(self) -> None:
        report = self.build_report()
        html = aw.build_html_email(self.build_config(), report, [])

        self.assertIn("report-sections", html)
        self.assertIn("summary-item", html)
        self.assertLess(html.index("Categories"), html.index("Category Sunburst"))
        self.assertLess(html.index("Category Sunburst"), html.index("Timeline (bar chart)"))
        self.assertLess(html.index("Timeline (bar chart)"), html.index("Top Window Titles"))
        self.assertLess(html.index("Top Window Titles"), html.index("Top Applications"))
        self.assertNotIn("cid:top-apps", html)
        self.assertNotIn("cid:top-titles", html)
        self.assertIn("metric-percent", html)

    def test_category_plot_uses_legend_for_labels(self) -> None:
        fig = aw.create_category_plot(
            {
                ("Work", "Dev", "Backend"): 1800.0,
                ("Work", "Dev", "Frontend"): 1800.0,
                ("Work", "Mail"): 1800.0,
                ("Comms", "Email"): 1800.0,
            }
        )

        self.assertEqual(len(fig.axes), 1)
        self.assertEqual(len(fig.axes[0].patches), 7)
        label_texts = " ".join(text.get_text() for text in fig.axes[0].texts)
        self.assertIn("Work", label_texts)
        self.assertIn("Dev", label_texts)
        self.assertIn("Comms", label_texts)
        self.assertIn("Mail", label_texts)
        self.assertIn("Backend", label_texts)

    def test_category_plot_spreads_outside_labels(self) -> None:
        fig = aw.create_category_plot(
            {
                ("Work", "Small A"): 300.0,
                ("Work", "Small B"): 300.0,
                ("Work", "Small C"): 300.0,
                ("Work", "Small D"): 300.0,
                ("Work", "Small E"): 300.0,
                ("Work", "Small F"): 300.0,
                ("Work", "Small G"): 300.0,
            }
        )

        self.assertEqual(len(fig.axes[0].lines), 7)

        outside_texts = [
            text
            for text in fig.axes[0].texts
            if math.hypot(*text.get_position()) > 1.1
        ]
        self.assertEqual(len(outside_texts), 7)

        radii = [math.hypot(*text.get_position()) for text in outside_texts]
        self.assertTrue(all(math.isclose(radius, 1.28, abs_tol=0.03) for radius in radii))

        for text in outside_texts:
            x, y = text.get_position()
            theta = math.atan2(y, x)
            if math.cos(theta) > 0:
                self.assertEqual(text.get_ha(), "left")
            elif math.cos(theta) < 0:
                self.assertEqual(text.get_ha(), "right")
            if math.sin(theta) > 0.35:
                self.assertEqual(text.get_va(), "bottom")
            elif math.sin(theta) < -0.35:
                self.assertEqual(text.get_va(), "top")

        for line in fig.axes[0].lines:
            xdata = list(line.get_xdata())
            ydata = list(line.get_ydata())
            self.assertEqual(len(xdata), 2)
            self.assertEqual(len(ydata), 2)
            self.assertTrue(math.isclose(math.atan2(ydata[0], xdata[0]), math.atan2(ydata[1], xdata[1]), abs_tol=1e-6))
            self.assertTrue(math.isclose(math.hypot(xdata[1], ydata[1]), 1.28, abs_tol=0.03))


if __name__ == "__main__":
    unittest.main()
