@echo off
rem Runs the Amazon morning watch locally (started by Windows Task Scheduler).
rem TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID come from the user's environment variables.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" amazon_bot.py >> amazon_bot.log 2>&1
