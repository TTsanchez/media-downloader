# -*- coding: utf-8 -*-
"""Сетевые операции: повторы, докачка и проверка контрольных сумм.

Отдельный модуль, чтобы одинаково качать wheel-архивы, FFmpeg и обновление
самого приложения: с Range-докачкой, ограничением времени и SHA-256.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import zipfile
from pathlib import Path

LOGGER = logging.getLogger("media_downloader.net")
USER_AGENT = {"User-Agent": "Media-Downloader"}
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)


class ChecksumError(RuntimeError):
    """Скачанный файл не совпал с ожидаемой контрольной суммой."""


def session():
    """HTTP-сеанс с повторами: первый запуск не должен падать из-за одного сбоя TLS."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    http = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.8,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    http.mount("https://", adapter)
    http.mount("http://", adapter)
    http.headers.update(USER_AGENT)
    return http


def get_json(url: str, timeout: float = 20, headers: dict | None = None):
    with session() as http:
        response = http.get(url, timeout=timeout, headers=headers or {})
        response.raise_for_status()
        return response.json()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_digest(value: str | None) -> str:
    """Приводит ``sha256:ABC`` или ``ABC`` к нижнему регистру без префикса."""
    return (value or "").strip().lower().removeprefix("sha256:")


def _human_size(size: float) -> str:
    if size < 1024:
        return f"{size:.0f} Б"
    if size < 1024 ** 2:
        return f"{size / 1024:.0f} КБ"
    megabytes = size / 1024 ** 2
    if megabytes < 10:
        return f"{megabytes:.1f} МБ"
    return f"{megabytes:.0f} МБ"


def _human_time(seconds: float) -> str:
    if seconds < 1:
        return "0 с"
    if seconds < 60:
        return f"{seconds:.0f} с"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} мин {rest:02d} с"


def download(
    url: str,
    destination: Path,
    log=None,
    progress=None,
    timeout=(20, 120),
    expected_sha256: str | None = None,
    retries: int = 3,
) -> Path:
    """Скачивает файл с докачкой и необязательной проверкой SHA-256.

    ``progress`` получает долю от 0 до 1 с учётом уже скачанной части.
    При обрыве ``.part`` сохраняется, поэтому следующая попытка (в этом или
    следующем запуске) продолжит с того же места.
    """
    emit = log or (lambda _message: None)
    report = progress or (lambda _fraction: None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    expected = normalize_digest(expected_sha256)
    LOGGER.info("Download started: url=%s destination=%s", url, destination)

    if expected and partial.exists():
        if sha256_file(partial) == expected:
            partial.replace(destination)
            emit(f"Загружено: {destination.name}")
            LOGGER.info("Download resumed and verified: destination=%s", destination)
            return destination

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        resumed_from = partial.stat().st_size if partial.exists() else 0
        if resumed_from:
            emit(f"Продолжаю загрузку {destination.name} с {_human_size(resumed_from)}...")
        try:
            headers = {"Accept-Encoding": "identity"}
            if resumed_from:
                headers["Range"] = f"bytes={resumed_from}-"
            with session() as http, http.get(url, stream=True, timeout=timeout, headers=headers) as response:
                if resumed_from and response.status_code == 416:
                    # Локальный файл уже целый или сервер не отдаёт диапазон.
                    partial.unlink(missing_ok=True)
                    raise IOError("Сервер отклонил докачку, начинаю заново")
                if resumed_from and response.status_code != 206:
                    LOGGER.info("Server ignored Range for %s; restarting download", url)
                    resumed_from = 0
                response.raise_for_status()

                total = 0
                if response.status_code == 206:
                    match = _CONTENT_RANGE.search(response.headers.get("content-range", ""))
                    if match and match.group(3) != "*":
                        total = int(match.group(3))
                if not total:
                    length = int(response.headers.get("content-length") or 0)
                    total = resumed_from + length if length else 0

                mode = "ab" if resumed_from and response.status_code == 206 else "wb"
                received = 0
                started = time.monotonic()
                last_emit = 0.0
                with partial.open(mode) as output:
                    for chunk in response.iter_content(1 << 20):
                        if not chunk:
                            continue
                        output.write(chunk)
                        received += len(chunk)
                        done = resumed_from + received
                        if total:
                            report(min(done / total, 1.0))
                        now = time.monotonic()
                        if now - last_emit >= 1.0:
                            last_emit = now
                            speed = received / max(now - started, 0.001)
                            if total:
                                detail = (f"{done * 100 // total}% · {_human_size(done)} из "
                                          f"{_human_size(total)} · {_human_size(speed)}/с")
                                if speed > 0:
                                    detail += f" · ~{_human_time((total - done) / speed)}"
                            else:
                                detail = f"{_human_size(done)} · {_human_size(speed)}/с"
                            emit(f"Скачивание {destination.name}: {detail}")
                if total and resumed_from + received != total:
                    raise IOError(f"Получено {resumed_from + received} из {total} байт")
            if expected:
                actual = sha256_file(partial)
                if actual != expected:
                    partial.unlink(missing_ok=True)
                    raise ChecksumError(
                        f"Контрольная сумма {destination.name} не совпала "
                        f"(ожидалось {expected[:12]}..., получено {actual[:12]}...)."
                    )
            partial.replace(destination)
            emit(f"Загружено: {destination.name}")
            LOGGER.info("Download completed: destination=%s", destination)
            return destination
        except ChecksumError:
            raise
        except Exception as error:
            last_error = error
            if partial.exists() and partial.stat().st_size == 0:
                partial.unlink(missing_ok=True)
            LOGGER.warning("Download attempt %s failed: %s", attempt, url, exc_info=True)
            if attempt < retries:
                emit("Сбой сети, повторяю загрузку...")
                time.sleep(2 * attempt)
    raise RuntimeError(f"Не удалось скачать {destination.name}: {last_error}")


def safe_extract_zip(archive: Path, destination: Path) -> None:
    """Распаковывает zip, не позволяя выйти за пределы папки назначения."""
    root = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        for entry in package.infolist():
            name = entry.filename.replace("\\", "/")
            if entry.is_dir():
                continue
            target = (root / name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Небезопасный путь в архиве: {entry.filename}")
        package.extractall(root)
