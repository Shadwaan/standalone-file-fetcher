@echo off
REM sff (Standalone File Fetcher) -- double-click launcher (Windows)
REM First run:  detects Python, creates a venv inside app\, installs dependencies.
REM Every run:  starts the FastAPI server on localhost:8899 and opens the browser.
REM Auto-shutdown: closes itself when you close the browser tab (~20s idle).

cd /d "%~dp0"
title sff -- Standalone File Fetcher

echo ========================================
echo  sff: Spotify -^> Rekordbox sync
echo  http://localhost:8899
echo ========================================
echo.

REM --- 1. Find Python ---------------------------------------------------------
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required, but none was found.
    echo.
    echo Easiest install ^(recommended^): https://www.python.org/downloads/
    echo   Download Python 3.13, run the installer, then double-click this file again.
    echo.
    echo   IMPORTANT: tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

REM --- 2. Bootstrap venv on first run -----------------------------------------
cd app

if not exist .venv\Scripts\python.exe (
    echo First-run setup: creating virtual environment and installing dependencies.
    echo This takes ~2 minutes on the first run; subsequent launches are instant.
    echo.
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo Installing Python packages...
    .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .venv\Scripts\pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo.
        echo ERROR: pip install failed. Scroll up for details.
        pause
        exit /b 1
    )
    echo Setup complete.
    echo.
)

REM --- 2b. Keep yt-dlp current -------------------------------------------------
REM The venv bootstrap above only runs pip when it CREATES .venv, so yt-dlp would
REM otherwise stay frozen at whatever shipped on first run. YouTube breaks stale
REM extractors within weeks (see DEBUG_LOG section 16), so re-check every launch.
REM Nightly (--pre) because YouTube fixes land there first. `yt-dlp -U` cannot be
REM used: it only self-updates the standalone binary, not a pip install.
REM Scoped to yt-dlp ONLY -- pyrekordbox must not silently move.
echo Checking for yt-dlp updates...
.venv\Scripts\python.exe -m pip install -U --pre -q --disable-pip-version-check --timeout 10 --retries 1 "yt-dlp[default]" || echo   (skipped - offline or PyPI unreachable; using installed version)
echo.

REM --- 3. Run the server ------------------------------------------------------
echo Server log appears below. Close the browser tab to stop the server
echo ^(or press Ctrl+C^). This window closes automatically when the server exits.
echo.
echo Browser opens automatically in 3 seconds...
echo.

REM Schedule browser to open after 3 seconds (gives uvicorn time to bind)
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8899"

set AUTO_SHUTDOWN_IDLE=20
.venv\Scripts\python.exe main.py
set EXIT_CODE=%errorlevel%

REM --- 4. Exit handling -------------------------------------------------------
REM Clean exit (0) or Ctrl+C -> close window silently
if %EXIT_CODE% equ 0 exit
if %EXIT_CODE% equ -1073741510 exit
if %EXIT_CODE% equ 3221225786 exit

echo.
echo Server exited with error %EXIT_CODE%. Press any key to close.
pause >nul
