# -*- coding: utf-8 -*-
"""История загрузок: небольшой JSON рядом с компонентами приложения.

Хранится только то, что нужно для списка: ссылка, название, канал, папка и
путь к файлу. Записи ограничены по количеству, файл пишется атомарно, чтобы
обрыв питания не оставил полупустой JSON.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from utils import data_root

LOGGER = logging.getLogger("media_downloader.history")

LIMIT = 300
_TEXT_FIELDS = ("url", "title", "channel", "duration", "thumb_url", "thumb_path",
                "file", "folder", "fmt", "source")


def history_file() -> Path:
    return data_root() / "history.json"


class History:
    def __init__(self, path: Path | str | None = None, limit: int = LIMIT):
        self.path = Path(path) if path else history_file()
        self.limit = max(1, int(limit))
        self._lock = threading.RLock()
        self._entries = self._load()

    # ---------- storage ----------

    def _load(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except Exception:
            LOGGER.warning("Could not read history; starting empty", exc_info=True)
            return []
        if not isinstance(raw, list):
            return []
        entries = []
        for item in raw:
            entry = self._clean(item)
            if entry:
                entries.append(entry)
            if len(entries) >= self.limit:
                break
        return entries

    @staticmethod
    def _clean(item) -> dict | None:
        if not isinstance(item, dict):
            return None
        entry = {}
        for field in _TEXT_FIELDS:
            value = item.get(field)
            entry[field] = str(value).strip() if value is not None else ""
        try:
            entry["size"] = max(0, int(item.get("size") or 0))
        except (TypeError, ValueError):
            entry["size"] = 0
        if not entry["title"] and not entry["url"]:
            return None
        if not entry["file"] and not entry["folder"]:
            return None
        entry["id"] = str(item.get("id") or uuid.uuid4().hex)
        entry["finished_at"] = str(item.get("finished_at") or datetime.now().isoformat(timespec="seconds"))
        return entry

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._entries, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except Exception:
            LOGGER.warning("Could not save history", exc_info=True)

    # ---------- public ----------

    def entries(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def add(self, entry: dict) -> dict | None:
        """Добавляет запись и возвращает её копию (или ``None``)."""
        cleaned = self._clean(entry)
        if not cleaned:
            return None
        with self._lock:
            self._entries.insert(0, cleaned)
            del self._entries[self.limit:]
            self._save()
        return dict(cleaned)

    def remove(self, entry_id: str) -> None:
        with self._lock:
            self._entries = [entry for entry in self._entries if entry.get("id") != entry_id]
            self._save()

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
