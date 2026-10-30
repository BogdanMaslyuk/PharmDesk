@echo off
setlocal
cd /d "%~dp0"
echo PharmDesk: checking for updates from GitHub...
echo IMPORTANT: close the PharmDesk server before running this file.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-windows.ps1"
if errorlevel 1 (
  echo.
  echo Update stopped. Read the error message above.
) else (
  echo.
  echo Update completed successfully.
)
pause
