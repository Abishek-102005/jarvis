@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    echo Launching JARVIS...
    .\.venv\Scripts\python.exe main.py
) else (
    echo .venv not found. Run:
    echo   py -3.12 -m venv .venv
    echo   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    echo   .\.venv\Scripts\python.exe main.py
    pause
)
