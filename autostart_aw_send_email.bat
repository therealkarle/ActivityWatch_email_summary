@echo off
setlocal

rem Wait 5 minutes after startup/login before sending the report.
timeout /t 300 /nobreak >nul

rem Run from the repository root so the Python script finds config.json correctly.
pushd "%~dp0"
python "activitywatch_email_summary.py" --once
set "EXIT_CODE=%ERRORLEVEL%"
popd

exit /b %EXIT_CODE%
