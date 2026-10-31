@echo off
REM Resume sff: start it the normal way, wait until it is up, then start a sync exactly as if you had pressed Sync.
REM
REM There is nothing to "resume" from: sff keeps its records after every finished playlist, and anything it already
REM downloaded is still in the Nicotine+ download folder, which it checks first. So a fresh sync carries on where the last
REM one stopped, using whatever the code is NOW (the YouTube fill-in, the duplicate guard, the to-buy list).
REM
REM Before double-clicking: close Rekordbox. Nicotine+ is started by sff itself.

cd /d "%~dp0"
title sff - resume

echo Starting sff...
start "sff" cmd /c start.bat

echo Waiting for it to come up (up to 4 minutes; it checks for a yt-dlp update first)...
powershell -NoProfile -Command "$up=$false; for($i=0;$i -lt 120;$i++){ try { Invoke-WebRequest http://127.0.0.1:8899/api/status -UseBasicParsing -TimeoutSec 2 | Out-Null; $up=$true; break } catch { Start-Sleep 2 } }; if(-not $up){ 'sff did not come up. Open start.bat by hand and press Sync.'; exit 1 }; Start-Sleep 4; try { $r = Invoke-WebRequest -Method Post http://127.0.0.1:8899/api/sync -UseBasicParsing; 'Sync started: ' + $r.Content } catch { 'Could not start the sync (is Rekordbox open, or is one already running?): ' + $_.Exception.Message; exit 1 }"

echo.
echo The sync is running in the sff window and in the browser tab that opened. Keep that browser tab open:
echo closing it stops sff 20 seconds later unless a sync is running (a running sync is never cut off).
echo.
pause
