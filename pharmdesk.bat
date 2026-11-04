@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0pharmdesk.ps1"
if errorlevel 1 (
  echo.
  echo PharmDesk menu failed. Please review the error above.
  pause
)
