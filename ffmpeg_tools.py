"""Подготовка FFmpeg для сборки и восстановление компонентов у пользователя."""

import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import net
from utils import get_ffmpeg_download


TOOLS = ("ffmpeg.exe", "ffprobe.exe")
LOGGER = logging.getLogger("media_downloader.ffmpeg")


def _archive_size(info: dict) -> str:
    size = int(info.get("size") or 0)
    return f" (~{size / (1024 * 1024):.0f} МБ)" if size else ""


def install_ffmpeg(destination: Path, log=None, progress=None) -> Path:
    """Скачивает Windows x64 сборку и сохраняет bin-файлы с библиотеками."""
    emit = log or (lambda _message: None)
    report = progress or (lambda _fraction: None)
    LOGGER.info("FFmpeg installation started: destination=%s", destination)
    info = get_ffmpeg_download()
    if not info:
        raise RuntimeError("Не удалось получить сборку FFmpeg. Проверьте подключение к GitHub.")
    emit(f"Скачиваю FFmpeg: {info['name']}{_archive_size(info)}")
    with tempfile.TemporaryDirectory(prefix="media-downloader-ffmpeg-") as folder:
        archive = Path(folder) / "ffmpeg.zip"
        net.download(
            info["url"],
            archive,
            log=emit,
            progress=report,
            timeout=(20, 120),
            expected_sha256=info.get("digest"),
        )
        staged = Path(folder) / "staged"
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                if entry.is_dir():
                    continue
                parts = entry.filename.replace("\\", "/").split("/")
                if len(parts) < 2 or parts[-2] != "bin":
                    continue
                name = parts[-1]
                # В shared-сборке рядом с exe лежат DLL: без них он не запустится.
                if name not in TOOLS and not name.lower().endswith(".dll"):
                    continue
                target = staged / "bin" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            for tool in TOOLS:
                if not (staged / "bin" / tool).is_file():
                    raise RuntimeError(f"В архиве не найден файл {tool}.")
            seen_licenses = set()
            for entry in package.infolist():
                name = Path(entry.filename).name
                if entry.is_dir() or name in seen_licenses:
                    continue
                if not name.lower().startswith(("license", "copying", "readme")):
                    continue
                seen_licenses.add(name)
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
            f"Archive checksum: {info.get('digest') or 'not provided'}\n", encoding="utf-8",
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
    report(1.0)
    emit("FFmpeg и ffprobe готовы.")
    LOGGER.info("FFmpeg installation completed: destination=%s", destination)
    return destination / "bin"
