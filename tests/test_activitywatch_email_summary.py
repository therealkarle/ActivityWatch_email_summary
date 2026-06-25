from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
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

    def test_first_run_date_is_initialized_on_first_start(self) -> None:
        sent_log = {"reports": {}}
        now = datetime(2026, 6, 25, 14, 45, tzinfo=self.tz)

        first_run_date, updated = aw.ensure_first_run_date(sent_log, now, self.tz)

        self.assertTrue(updated)
        self.assertEqual(first_run_date.isoformat(), "2026-06-25")
        self.assertEqual(sent_log["first_run_date"], "2026-06-25")

    def test_enumerate_periods_keeps_calendar_key_when_clipped(self) -> None:
        start = datetime(2026, 6, 25, 0, 0, tzinfo=self.tz)
        end = datetime(2026, 6, 25, 18, 0, tzinfo=self.tz)

        periods = aw.enumerate_periods("weekly", start, end, self.tz, aw.parse_week_start_day("mon"))

        self.assertEqual(len(periods), 1)
        self.assertEqual(periods[0].start, start)
        self.assertEqual(periods[0].key, "2026-06-22")
        self.assertEqual(periods[0].label, "2026-06-22")


if __name__ == "__main__":
    unittest.main()
