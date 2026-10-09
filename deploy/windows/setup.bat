@echo off
REM Double-click convenience wrapper for first-time setup.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
echo.
pause
endlocal
