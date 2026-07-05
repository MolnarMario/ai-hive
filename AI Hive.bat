@echo off
rem Launch AI Hive with the project venv (pythonw = no console window).
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo The project venv is missing. Create it first:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "AI Hive" /D "%~dp0" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
