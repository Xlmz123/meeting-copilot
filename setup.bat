@echo off
cd /d "%~dp0"
echo Setting up Meeting Copilot...
where py >nul 2>nul || (echo Python not found. Install Python 3.12 from python.org first. & pause & exit /b 1)
py -3.12 -c "1" >nul 2>nul && (set PY=py -3.12) || (set PY=py -3)
%PY% -m venv .venv || (echo Could not create venv & pause & exit /b 1)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\pip install -r requirements.txt || (echo Install failed - see errors above & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo.
echo Done. Paste your Groq (and optional NVIDIA) keys into the .env file that's opening now, save it, then run start.bat
notepad .env
