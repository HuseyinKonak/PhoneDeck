@echo off
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw.exe -m phonedeck
