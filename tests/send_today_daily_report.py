from __future__ import annotations

from datetime import datetime
from pathlib import Path

import activitywatch_email_summary as aw


ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"


def main() -> int:
    aw.setup_logging()

    try:
        config = aw.load_config(CONFIG_FILE)
    except Exception as exc:
        aw.LOGGER.error("Fehler beim Laden der Konfiguration: %s", exc)
        return 1

    now = datetime.now(config.timezone)
    day_start = aw.align_start_to_timeframe(now, "daily", config.timezone)
    period_key, period_label = aw.build_period_metadata(day_start, "daily", config.timezone)
    period = aw.ReportPeriod(
        timeframe="daily",
        start=day_start,
        end=now,
        label=period_label,
        key=period_key,
    )

    try:
        bucket_catalog = aw.discover_buckets(config.aw_api_base_url)
        report = aw.build_report(config, bucket_catalog, period)
        subject, html_body, inline_images = aw.render_email(config=config, report=report)
        aw.send_email(config.smtp_settings, subject, html_body, inline_images)
    except Exception as exc:
        aw.LOGGER.exception("Fehler beim Senden des heutigen Daily-Reports: %s", exc)
        return 1

    aw.LOGGER.info("Heutiger Daily-Report gesendet: %s", period.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
