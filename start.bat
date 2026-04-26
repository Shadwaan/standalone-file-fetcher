@echo off
REM sff (Standalone File Fetcher) — double-click launcher
REM Starts the FastAPI server on localhost:8899 and opens the browser.
REM Close this window (or press Ctrl+C) to stop the server.

cd /d "%~dp0"
title sff — Standalone File Fetcher

echo ========================================
echo  sff: Spotify -^> Rekordbox sync
echo  http://localhost:8899
echo ========================================
echo.
echo Server log appears below. Close this window (or Ctrl+C) to stop.
echo Browser opens automatically in 3 seconds...
echo.

REM Schedule browser to open after 3 seconds (gives uvicorn time to bind the port)
start "" /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8899"

REM Run the server in the foreground so logs are visible
python main.py

REM If python exits, pause so user can read any error
echo.
echo Server stopped. Press any key to close.
pause >nul
