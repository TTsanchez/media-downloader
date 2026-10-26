"""Подготовка FFmpeg для сборки и восстановление компонентов у пользователя."""

import hashlib
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests

from utils import get_ffmpeg_download


TOOLS = ("ffmpeg.exe", "ffprobe.exe")
LOGGER = logging.getLogger("media_downloader.ffmpeg")


def install_ffmpeg(destination: Path, log=None) -> Path:
    """Загружает статическую Windows x64 сборку и сохраняет только нужные файлы."""
    emit = log or (lambda _message: None)
    LOGGER.info("FFmpeg installation started: destination=%s", destination)
    info = get_ffmpeg_download()
    if not info:
        raise RuntimeError("Не удалось получить сборку FFmpeg. Проверьте подключение к GitHub.")
    emit(f"Скачиваю компоненты обработки видео: {info['name']}")
    with tempfile.TemporaryDirectory(prefix="media-downloader-ffmpeg-") as folder:
        archive = Path(folder) / "ffmpeg.zip"
        digest = hashlib.sha256()
        with requests.get(info["url"], stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or info.get("size") or 0)
            received, last_percent = 0, -1
            with archive.open("wb") as output:
                for chunk in response.iter_content(1 << 20):
                    if not chunk:
                        continue
                    output.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    percent = min(received * 100 // total, 100) if total else 0
                    if percent // 10 != last_percent // 10:
                        emit(f"Загрузка FFmpeg: {percent}%")
                        last_percent = percent
        expected = info.get("digest")
        actual = "sha256:" + digest.hexdigest()
        if expected and expected != actual:
            LOGGER.error("FFmpeg checksum mismatch: expected=%s actual=%s", expected, actual)
            raise RuntimeError("Контрольная сумма FFmpeg не совпала. Повторите загрузку.")
        staged = Path(folder) / "staged"
        with zipfile.ZipFile(archive) as package:
            for tool in TOOLS:
                matches = [entry for entry in package.infolist() if not entry.is_dir()
                           and Path(entry.filename).name == tool]
                if len(matches) != 1:
                    raise RuntimeError(f"В архиве не найден единственный файл {tool}.")
                target = staged / "bin" / tool
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(matches[0]) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            for entry in package.infolist():
                name = Path(entry.filename).name
                if not entry.is_dir() and name.lower().startswith(("license", "copying", "readme")):
                    target = staged / "licenses" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(entry) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
        (staged / "licenses").mkdir(exist_ok=True)
        (staged / "licenses" / "SOURCE.txt").write_text(
            "FFmpeg and ffprobe are separate programs distributed with Media Downloader.\n"
            "Their licenses are supplied in this directory.\n"
            f"Binary archive: {info['url']}\nRelease: {info.get('release_url', '')}\n"
            "Build recipes and corresponding source references: https://github.com/yt-dlp/FFmpeg-Builds\n"
            "FFmpeg source: https://github.com/yt-dlp/FFmpeg\n"
            f"Archive checksum: {actual}\n", encoding="utf-8",
        )
        (staged / "manifest.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        destination.mkdir(parents=True, exist_ok=True)
        for file in staged.rglob("*"):
            if file.is_file():
                target = destination / file.relative_to(staged)
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".part")
                shutil.copyfile(file, temporary)
                temporary.replace(target)
    emit("FFmpeg и ffprobe готовы.")
    LOGGER.info("FFmpeg installation completed: destination=%s", destination)
    return destination / "bin"
