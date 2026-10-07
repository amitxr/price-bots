@echo off
rem Opens the dashboard in the browser. The server normally starts at logon (the "Amazon dashboard"
rem scheduled task); if it is not running, it is started here first, in the background.
cd /d "%~dp0"
powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue)) { Start-Process '.venv\Scripts\pythonw.exe' 'dashboard\server.py' -WorkingDirectory '%~dp0.'; Start-Sleep -Seconds 2 }"
start "" http://127.0.0.1:8765
