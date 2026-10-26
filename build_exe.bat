@echo off
chcp 65001 > nul
title Media Downloader - build
cd /d "%~dp0"

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
"%BUILD_PYTHON%" -m pip install -q -r requirements.txt
if errorlevel 1 goto :fail

echo Подготавливаю встроенные FFmpeg и ffprobe...
"%BUILD_PYTHON%" prepare_ffmpeg.py
if errorlevel 1 goto :fail

echo.
echo Собираю .exe (это 1-3 минуты)...
echo.

set BUILD_ARGS=--noconfirm --clean --onefile --windowed ^
    --name "Media-Downloader" ^
    --add-data "version.txt;." ^
    --add-binary ".build-tools\ffmpeg\bin\ffmpeg.exe;ffmpeg/bin" ^
    --add-binary ".build-tools\ffmpeg\bin\ffprobe.exe;ffmpeg/bin" ^
    --add-data ".build-tools\ffmpeg\licenses;ffmpeg/licenses" ^
    --add-data ".build-tools\ffmpeg\manifest.json;ffmpeg" ^
    --collect-all ttkbootstrap ^
    --collect-all yt_dlp ^
    --collect-all yt_dlp_ejs ^
    --collect-all yt_dlp_plugins ^
    --hidden-import=yt_dlp_plugins.extractor.getpot_bgutil_http ^
    --hidden-import=yt_dlp_plugins.extractor.getpot_bgutil_script

if exist "icon.ico" (
    "%BUILD_PYTHON%" -m PyInstaller %BUILD_ARGS% --icon="icon.ico" --add-data "icon.ico;." main.py
) else (
    "%BUILD_PYTHON%" -m PyInstaller %BUILD_ARGS% main.py
)

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
