@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0control-windows.ps1"
if errorlevel 1 (
  echo.
  echo PharmDesk control center stopped with an error.
  pause
)
