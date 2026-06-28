# ActivityWatch Email Summary

ActivityWatch Email Summary is an automatic time tracking report tool that turns local ActivityWatch data into scheduled SMTP email reports. It creates readable daily, weekly, monthly, and yearly ActivityWatch reports with category breakdowns, charts, top applications, top window titles, and a timeline view.

It is designed for:
- people who want a personal productivity report from ActivityWatch
- Windows users who want a simple autostart workflow
- GitHub repositories that need clear setup instructions
- AI agents and automation tools that need a concise, machine-friendly project summary

## Quick Facts

- Input: ActivityWatch window events from a local ActivityWatch instance
- Output: HTML email with plain-text fallback and inline PNG charts
- Reports: daily, weekly, monthly, yearly
- Delivery: SMTP with TLS or SSL
- Safety: duplicate-send protection via a local sent log
- Runtime files: stored in `.aw_email_summary/`
- Main script: `activitywatch_email_summary.py`
- One-off helper: `tests/send_today_daily_report.py`

## Table of Contents

- [Features](#features)
- [Repository Contents](#repository-contents)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Windows Autostart](#windows-autostart)
- [GitHub Publishing Guide](#github-publishing-guide)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Security and Privacy](#security-and-privacy)

## Features

- Reads activity data from the local ActivityWatch API.
- Discovers ActivityWatch window buckets automatically.
- Uses canonical ActivityWatch categories when available.
- Falls back to raw window-event data when canonical categories are unavailable.
- Supports `daily`, `weekly`, `monthly`, and `yearly` reporting.
- Builds a category hierarchy view with percentages.
- Generates a category sunburst chart.
- Generates a timeline bar chart.
- Shows top applications and top window titles.
- Sends a multipart email with:
  - plain-text body
  - HTML body
  - inline PNG charts
- Avoids duplicate sends by storing report state in a local sent log.
- Writes logs to a local runtime directory.
- Includes Windows startup scripts for boot-time or logon execution.
- Includes a helper script for sending the most recent completed daily report.

## Repository Contents

- `activitywatch_email_summary.py` - main application script
- `tests/send_today_daily_report.py` - helper that sends the last completed daily report
- `tests/test_activitywatch_email_summary.py` - unit tests
- `config.example.json` - sample configuration file
- `autostart_aw_send_email.bat` - Windows batch launcher
- `autostart_aw_send_email.vbs` - hidden-window launcher for Windows autostart
- `BSPs/EmailLayout1.docx` - bundled document asset
- `.gitignore` - ignores local config, runtime data, and generated files

## Requirements

- Python 3.10 or newer
- A local ActivityWatch instance
- An SMTP account and credentials that can send email
- `matplotlib` for chart generation

The code talks directly to the ActivityWatch HTTP API. It does not require a separate Python ActivityWatch client library.

## Installation

1. Clone the repository.
2. Create and activate a virtual environment:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

3. Install the runtime dependency:

```powershell
python -m pip install matplotlib
```

4. For test execution, no extra package is required because the tests use the Python standard library `unittest`.
5. Copy the example configuration:

```powershell
Copy-Item config.example.json config.json
```

6. Edit `config.json` and fill in your real SMTP and ActivityWatch settings.

## Configuration

The application reads `config.json` from the repository root. Keep this file out of version control. It is already ignored by `.gitignore`.

### Top-Level Settings

| Setting | Type | Description |
| --- | --- | --- |
| `enabled_timeframes` | array of strings | Reports to generate. Allowed values: `daily`, `weekly`, `monthly`, `yearly`. |
| `top_items_limit` | integer | Number of top applications and titles to show. Must be at least 1. |
| `lookback_days` | integer | Historical window to scan for pending reports. Must be at least 1. |
| `week_start_day` | string or integer | First day of the week. Use `mon` to `sun` or `0` to `6`. |
| `aw_api_base_url` | string | Base URL of the local ActivityWatch API. Default: `http://localhost:5600`. |
| `timezone` | string | IANA timezone name, for example `Europe/Berlin`. |
| `smtp_settings` | object | SMTP connection and login details. |

### SMTP Settings

| Setting | Type | Description |
| --- | --- | --- |
| `server` | string | SMTP server host name. |
| `port` | integer | SMTP port. |
| `sender_email` | string | From address used in the email header. |
| `recipient_email` | string | Destination email address. |
| `username` | string | SMTP login user name. |
| `password` | string | SMTP password or app password. |
| `use_tls` | boolean | Use STARTTLS after connecting. Default: `true`. |
| `use_ssl` | boolean | Use SMTP over SSL. Default: `false`. |

### Example

```json
{
  "enabled_timeframes": ["daily", "weekly", "monthly", "yearly"],
  "top_items_limit": 5,
  "lookback_days": 30,
  "week_start_day": "mon",
  "aw_api_base_url": "http://localhost:5600",
  "timezone": "Europe/Berlin",
  "smtp_settings": {
    "server": "smtp.gmail.com",
    "port": 587,
    "sender_email": "YOUR_SENDER_EMAIL",
    "recipient_email": "YOUR_RECIPIENT_EMAIL",
    "username": "YOUR_SMTP_USERNAME",
    "password": "YOUR_APP_PASSWORD",
    "use_tls": true,
    "use_ssl": false
  }
}
```

## Usage

All commands below assume you run them from the repository root.

### Normal Run

```powershell
python activitywatch_email_summary.py
```

The script runs once by default, scans for unreported periods, and sends any pending reports.

### Explicit One-Time Run

```powershell
python activitywatch_email_summary.py --once
```

This is functionally the same as the default run, but it makes the intended behavior explicit.

### Send the Most Recent Completed Daily Report

```powershell
python tests/send_today_daily_report.py
```

This helper sends the last completed daily report directly, without iterating over all pending timeframes.

## Windows Autostart

Two startup helpers are included for Windows:

- `autostart_aw_send_email.bat` waits 5 minutes after login/startup and then runs the main script once.
- `autostart_aw_send_email.vbs` launches the batch file with a hidden console window.

### Recommended Setup

1. Place the repository in a stable location.
2. Make sure `config.json` exists in the repository root.
3. Create a shortcut to `autostart_aw_send_email.vbs`.
4. Put that shortcut into the Windows Startup folder.

The batch file uses `pushd "%~dp0"` so the Python script is started from the repository root and can find `config.json`.

## GitHub Publishing Guide

If you want to publish the project on GitHub, keep the repository clean and only commit the source files and documentation.

### What to Commit

- `activitywatch_email_summary.py`
- `tests/`
- `config.example.json`
- `autostart_aw_send_email.bat`
- `autostart_aw_send_email.vbs`
- `BSPs/EmailLayout1.docx`
- `.gitignore`
- `README.md`

### What to Keep Local

- `config.json`
- `.aw_email_summary/`
- generated logs
- generated sent-log state
- `__pycache__/`
- local shortcuts such as `.lnk` files

### GitHub Init and Push

If this is a new repository:

```powershell
git init
git add .
git commit -m "Add ActivityWatch email summary"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

If the repository already exists, just commit the README and push the branch you are working on.

### Suggested GitHub Description

`ActivityWatch Email Summary turns local ActivityWatch data into automatic daily, weekly, monthly, and yearly email reports with charts, category breakdowns, and SMTP delivery.`

This description works well for GitHub search, SEO, and AI agents because it names the main function, data source, report types, and output format.

## Testing

Run the unit tests with the Python standard library:

```powershell
python -m unittest discover -s tests
```

You can also run an individual test file:

```powershell
python -m unittest tests.test_activitywatch_email_summary
```

The test suite covers:

- weekly start-day handling
- period alignment and metadata
- first-run initialization
- canonical category fallback behavior
- HTML layout and chart behavior
- top-list rendering

## Troubleshooting

### `config.json` is missing

- Copy `config.example.json` to `config.json`.
- Fill in valid SMTP credentials and an ActivityWatch API URL.

### SMTP login fails

- Check `server`, `port`, `username`, and `password`.
- If your provider requires an app password, use that instead of your normal account password.
- Make sure `use_tls` and `use_ssl` match the provider's expectations.

### ActivityWatch API is unavailable

- Confirm ActivityWatch is running locally.
- Verify the API URL in `aw_api_base_url`.
- The default is `http://localhost:5600`.

### No window bucket is found

- Make sure the ActivityWatch window watcher is active.
- The script looks for bucket names containing `aw-watcher-window` or `window`.

### No report is sent

- The period may have no active data.
- The script marks empty periods as completed so they are not retried forever.
- Check the runtime log in `.aw_email_summary/activitywatch_email_summary.log`.

### Timeframe or week-start errors

- Use only `daily`, `weekly`, `monthly`, or `yearly` in `enabled_timeframes`.
- Use `mon` to `sun`, or `0` to `6`, for `week_start_day`.

## Security and Privacy

- `config.json` contains SMTP credentials and should stay local.
- Runtime state and logs are written to `.aw_email_summary/` and are ignored by Git.
- The application only reads data from your local ActivityWatch instance, but that data can still contain sensitive activity details.
- If you share screenshots or logs, review them first because window titles and application names may reveal personal information.

## How It Works

1. The script loads `config.json`.
2. It discovers ActivityWatch window and AFK buckets from the local API.
3. It determines which report periods have already been processed.
4. It collects window events for each pending period.
5. It aggregates time by application, title, category, and timeline bucket.
6. It renders charts and HTML.
7. It sends the email through SMTP.
8. It writes the sent-log entry so the same report is not sent twice.

## License

Add your project license here if you plan to publish the repository publicly.
