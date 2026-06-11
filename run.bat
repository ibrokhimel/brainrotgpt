@echo off
cd /d "%~dp0"
title BrainrotGPT

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up virtual environment...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    echo.
)

echo Starting BrainrotGPT...
echo (close this window or press Ctrl+C to stop the bot)
echo.
".venv\Scripts\python.exe" bot.py

echo.
echo Bot stopped. Press any key to close.
pause >nul
