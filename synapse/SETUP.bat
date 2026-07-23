@echo off
REM ============================================================
REM  Synapse - one-time setup (works from any folder)
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/3] Creating virtual environment (.venv)...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: python not found on PATH. Install Python 3.10+ first.
        pause
        exit /b 1
    )
) else (
    echo     already exists, skipping.
)

echo [2/3] Upgrading pip...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip

echo [3/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency install failed - see output above.
    pause
    exit /b 1
)

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from template - EDIT IT and add your API keys.
) else (
    echo .env already exists, leaving it alone.
)

echo.
echo Setup complete. Next:
echo   1. Edit .env with your keys (ZenMux / Freebuff / Colibri)
echo   2. Run START.bat - server + UI open on http://127.0.0.1:8000
pause
