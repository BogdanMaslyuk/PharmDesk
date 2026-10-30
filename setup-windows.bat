@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if errorlevel 1 goto :NO_PY
py -3 -c "import sys; assert sys.version_info >= (3, 11)" >nul 2>&1
if errorlevel 1 goto :PY_VERSION

echo [1/3] Creating the Python environment...
py -3 -m venv ".venv"
if errorlevel 1 goto :VENV_FAIL

echo [2/3] Installing requirements...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto :INSTALL_FAIL

echo [3/3] Preparing example data...
".venv\Scripts\python.exe" -m app.cli seed-demo
if errorlevel 1 goto :DATA_FAIL

echo.
echo Setup complete! Run start-windows.bat
pause
exit /b 0

:NO_PY
echo ERROR: Python launcher not found. Install Python 3.11 or newer from python.org.
goto :FAILED
:PY_VERSION
echo ERROR: Python 3.11 or newer is required.
goto :FAILED
:VENV_FAIL
echo ERROR: Could not create the Python environment.
goto :FAILED
:INSTALL_FAIL
echo ERROR: Could not install requirements. Check network and Python version.
goto :FAILED
:DATA_FAIL
echo ERROR: Could not initialize the example database.
goto :FAILED
:FAILED
echo.
echo Setup failed. See the message above.
pause
exit /b 1
