@echo off
setlocal
cd /d "%~dp0"
echo PharmDesk inventory repair - CLOSE THE SERVER FIRST.
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0replace-inventory-windows.ps1"
if errorlevel 1 (
 echo.
 echo Repair did not finish. Read the error above.
)
pause
