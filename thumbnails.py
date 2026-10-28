# -*- coding: utf-8 -*-
"""Превью видео: адрес обложки, загрузка в кэш и подготовка картинки для Tk.

Обложки YouTube отдаются в JPEG/WebP, а Tkinter умеет только PNG/GIF, поэтому
картинки обрабатываются через Pillow. Файлы кэшируются в
``%LOCALAPPDATA%\\Media-Downloader\\cache\\thumbs`` и переиспользуются между
запусками: повторно открывать одну и ту же ссылку без сети.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

import net
from utils import data_root

LOGGER = logging.getLogger("media_downloader.thumbnails")

MAX_BYTES = 6 * 1024 * 1024
CACHE_LIMIT = 400
CACHE_SUFFIX = ".img"

# youtu.be/ID, watch?v=ID, shorts/ID, embed/ID, live/ID
_YOUTUBE_ID = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})"
)


def cache_dir() -> Path:
    return data_root() / "cache" / "thumbs"


def cache_key(url: str) -> str:
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:32]


def cached_path(url: str) -> Path | None:
    """Возвращает файл из кэша, если он уже скачан."""
    if not url:
        return None
    path = cache_dir() / (cache_key(url) + CACHE_SUFFIX)
    try:
        return path if path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


def guess_thumbnail(url: str) -> str:
    """Обложка YouTube по ссылке, без обращения к сети.

    mqdefault.jpg есть у любого публичного ролика, поэтому очередь получает
    превью сразу, не дожидаясь извлечения полной информации через yt-dlp.
    """
    match = _YOUTUBE_ID.search(url or "")
    if not match:
        return ""
    return f"https://i.ytimg.com/vi/{match.group(1)}/mqdefault.jpg"


def _valid_image(path: Path) -> bool:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        return True
    except Exception:
        return False


def fetch(url: str, timeout: int = 25) -> Path | None:
    """Скачивает обложку в кэш. Возвращает путь или ``None`` при неудаче."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    existing = cached_path(url)
    if existing:
        return existing
    destination = cache_dir() / (cache_key(url) + CACHE_SUFFIX)
    partial = destination.with_suffix(destination.suffix + ".part")
    try:
        cache_dir().mkdir(parents=True, exist_ok=True)
        net.download(url, destination, timeout=(10, timeout), retries=2)
        if destination.stat().st_size > MAX_BYTES or not _valid_image(destination):
            destination.unlink(missing_ok=True)
            return None
        return destination
    except Exception:
        LOGGER.info("Thumbnail download failed: %s", url, exc_info=True)
        destination.unlink(missing_ok=True)
        partial.unlink(missing_ok=True)
        return None


def load(path: Path | str | None, size=(224, 126), radius: int = 10):
    """Открывает картинку, обрезает по центру и скругляет углы.

    Возвращает ``PIL.Image`` в RGBA или ``None``: решение о показе принимает
    интерфейс, а не сеть.
    """
    if not path:
        return None
    try:
        from PIL import Image, ImageDraw, ImageOps

        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(image, size, Image.Resampling.LANCZOS)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255
        )
        image = image.convert("RGBA")
        image.putalpha(mask)
        return image
    except Exception:
        LOGGER.debug("Could not open thumbnail: %s", path, exc_info=True)
        return None


def placeholder(size=(224, 126), radius: int = 10, background="#222833", accent="#4d8dff"):
    """Заглушка с треугольником «play» для карточек без обложки."""
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGBA", size, background)
        draw = ImageDraw.Draw(image)
        width, height = size
        triangle = [
            (width * 0.44, height * 0.32),
            (width * 0.44, height * 0.68),
            (width * 0.66, height * 0.50),
        ]
        draw.polygon(triangle, fill=accent)
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=radius, fill=255
        )
        image.putalpha(mask)
        return image
    except Exception:
        return None


def prune(limit: int = CACHE_LIMIT) -> int:
    """Удаляет самые старые обложки, чтобы кэш не рос бесконечно."""
    folder = cache_dir()
    try:
        files = sorted(
            (item for item in folder.iterdir() if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0
    removed = 0
    for old in files[limit:]:
        try:
            old.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    if removed:
        LOGGER.info("Pruned %s old thumbnails", removed)
    return removed
