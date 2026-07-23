@echo off
REM Starts the HexStrike AI server (no console window).
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
start "" /min "hexstrike-env\Scripts\python.exe" hexstrike_server.py
echo Server starting on http://127.0.0.1:8888/  (give it ~10 seconds)
timeout /t 3 >nul
start "" "http://127.0.0.1:8888/"
