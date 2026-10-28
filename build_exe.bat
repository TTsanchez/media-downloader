@echo off
chcp 65001 > nul
title Media Downloader - build
cd /d "%~dp0"
set "PYTHONUTF8=1"

echo ==========================================
echo   Media Downloader - сборка .exe
echo ==========================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo Создаю виртуальное окружение...
    python -m venv venv
    if errorlevel 1 goto :fail
)

set "BUILD_PYTHON=%~dp0venv\Scripts\python.exe"

echo Ставлю зависимости...
"%BUILD_PYTHON%" -m pip install -q --upgrade pip
if errorlevel 1 goto :fail
"%BUILD_PYTHON%" -m pip install -q -r requirements-build.txt
if errorlevel 1 goto :fail

echo Проверяю кодировку исходников...
"%BUILD_PYTHON%" verify_encoding.py
if errorlevel 1 goto :fail

echo.
echo Собираю .exe (это 1-3 минуты)...
echo.

"%BUILD_PYTHON%" -m PyInstaller --noconfirm --clean Media-Downloader.spec
if errorlevel 1 goto :fail

echo.
echo ==========================================
echo   Готово: dist\Media-Downloader.exe
echo ==========================================
echo.
pause
exit /b 0

:fail
echo.
echo [ОШИБКА] Сборка не удалась. Смотрите текст выше.
pause
exit /b 1
