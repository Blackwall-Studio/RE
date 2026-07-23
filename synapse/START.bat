@echo off
REM Starts Synapse (server + browser UI).
REM Usage: START.bat [port]   (default 8000)
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PORT=%~1
if "%PORT%"=="" set PORT=8000
".venv\Scripts\python.exe" run.py --port %PORT%
