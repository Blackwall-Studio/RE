@echo off
REM ============================================================
REM  HexStrike AI - one-time setup (works from any folder)
REM  Creates the local python environment and installs deps.
REM  Safe to re-run; it only does what's missing.
REM ============================================================
setlocal
cd /d "%~dp0"

echo [1/3] Creating virtual environment (hexstrike-env)...
if not exist "hexstrike-env\Scripts\python.exe" (
    python -m venv hexstrike-env
    if errorlevel 1 (
        echo ERROR: python not found on PATH. Install Python 3.10+ first.
        pause
        exit /b 1
    )
) else (
    echo     already exists, skipping.
)

echo [2/3] Upgrading pip...
"hexstrike-env\Scripts\python.exe" -m pip install --quiet --upgrade pip

echo [3/3] Installing dependencies (core set)...
"hexstrike-env\Scripts\python.exe" -m pip install --quiet flask requests psutil fastmcp beautifulsoup4 selenium webdriver-manager aiohttp "bcrypt==4.0.1" mitmproxy
if errorlevel 1 (
    echo WARNING: some core deps failed - check the output above.
)

echo.
echo Optional heavy python deps (angr + pwntools, needs C++ build tools)...
"hexstrike-env\Scripts\python.exe" -m pip install --quiet pwntools angr 2>nul
if errorlevel 1 (
    echo     skipped (optional - the server does not require them).
) else (
    echo     installed.
)

echo.
echo Setup complete. Next:
echo   1. (optional, recommended) powershell -ExecutionPolicy Bypass -File Install-Tools.ps1
echo      ^ downloads the 25+ security tools into tools\bin and updates your PATH
echo   2. Start the server:  HexStrikeControl.exe  (or run-server.bat)
echo   3. Open the control page:  http://127.0.0.1:8888/
pause
