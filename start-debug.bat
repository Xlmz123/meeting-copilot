@echo off
REM Same as start.bat but keeps this window open with live log messages (for troubleshooting).
cd /d "%~dp0"
if not exist .venv (echo Run setup.bat first. & pause & exit /b 1)
.venv\Scripts\python copilot.py
pause
