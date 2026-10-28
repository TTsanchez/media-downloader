# -*- coding: utf-8 -*-
"""Готовит текст релиза из раздела CHANGELOG.md для текущей версии.

Используется в release-workflow, чтобы тело релиза совпадало с журналом
изменений, а не собиралось из сообщений коммитов.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_FILE = ROOT / "version.txt"

_HEADING = re.compile(r"^##\s+\[?v?([0-9][^\]]*?)\]?\s*(?:[—–-].*)?$")


def read_version(path: Path = VERSION_FILE) -> str:
    """Возвращает версию без префикса ``v``."""
    try:
        return path.read_text(encoding="utf-8").strip().lstrip("vV")
    except OSError:
        return ""


def extract_notes(text: str, version: str) -> str:
    """Вырезает раздел указанной версии из changelog.

    Возвращает пустую строку, если раздела нет. Заголовок следующей версии
    и горизонтальные разделители в конце не включаются.
    """
    version = (version or "").strip().lstrip("vV")
    if not version:
        return ""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = None
    for index, line in enumerate(lines):
        match = _HEADING.match(line.strip())
        if match and match.group(1).strip().lstrip("vV") == version:
            start = index + 1
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        if _HEADING.match(lines[index].strip()):
            end = index
            break
    body = lines[start:end]
    while body and (not body[-1].strip() or body[-1].strip() in ("---", "***", "___")):
        body.pop()
    while body and not body[0].strip():
        body.pop(0)
    return "\n".join(body).strip()


def build_notes(changelog: Path = CHANGELOG, version: str | None = None) -> str:
    version = version if version is not None else read_version()
    try:
        text = changelog.read_text(encoding="utf-8")
    except OSError as error:
        raise SystemExit(f"Не удалось прочитать {changelog.name}: {error}") from error
    return extract_notes(text, version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Тело релиза из CHANGELOG.md")
    parser.add_argument("-o", "--output", type=Path, help="файл для записи (по умолчанию stdout)")
    parser.add_argument("--version", help="версия (по умолчанию из version.txt)")
    parser.add_argument("--allow-missing", action="store_true",
                        help="не завершаться с ошибкой, если раздела нет")
    args = parser.parse_args(argv)

    version = (args.version or read_version()).strip().lstrip("vV")
    notes = build_notes(version=version)
    if not notes:
        message = f"В CHANGELOG.md нет раздела для версии {version or '(неизвестно)'}."
        if not args.allow_missing:
            print(message, file=sys.stderr)
            return 1
        notes = "Полный список изменений: CHANGELOG.md."
    if args.output:
        args.output.write_text(notes + "\n", encoding="utf-8")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
