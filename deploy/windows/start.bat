@echo off
REM Double-click convenience wrapper. Opens the console in your browser.
setlocal
cd /d "%~dp0"
start "" http://localhost:8000
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
endlocal
