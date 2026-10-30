@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -m venv .venv
if errorlevel 1 (
 echo Установите Python 3.11+ и отметьте Add Python to PATH.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
 echo Не удалось установить зависимости. Проверьте подключение к интернету.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m app.cli seed-demo
echo Готово. Запустите start-windows.bat
pause
