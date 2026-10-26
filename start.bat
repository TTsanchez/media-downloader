@echo off
chcp 65001 > nul
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo.
    echo  Первый запуск. Создаю окружение, это может занять пару минут...
    echo.
    python -m venv venv || goto :fail
    "venv\Scripts\python.exe" -m pip install -q --upgrade pip || goto :fail
)

fc /b requirements.txt venv\requirements-installed.txt >nul 2>nul
if errorlevel 1 (
    "venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :fail
    copy /y requirements.txt venv\requirements-installed.txt >nul
)

start "" "venv\Scripts\pythonw.exe" main.py
exit /b 0

:fail
echo.
echo  Не удалось. Проверьте, что Python установлен и доступен в PATH.
echo.
pause
exit /b 1
