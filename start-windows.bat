@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto :SETUP_NEEDED

echo Starting PharmDesk...
echo In your browser, open: http://127.0.0.1:8000
echo Press Ctrl+C to stop the app.
echo.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
if errorlevel 1 goto :START_FAIL
pause
exit /b 0

:SETUP_NEEDED
echo ERROR: Python environment not found. Run setup-windows.bat first.
goto :FAILED
:START_FAIL
echo ERROR: Server could not start.
goto :FAILED
:FAILED
pause
exit /b 1
