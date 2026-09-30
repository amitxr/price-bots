@echo off
rem Starts the local dashboard and opens it in the browser. Close this window to stop it.
cd /d "%~dp0"
start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" dashboard\server.py
