"""Проверяет, что исходники — настоящий UTF-8 без «кракозябр».

Такие файлы ломают русский интерфейс в собранном .exe, поэтому сборка
должна падать, а не выпускать релиз с испорченным текстом.
"""

import pathlib
import sys

MARKERS = tuple(
    chr(first) + chr(second)
    for first, second in (
        (0x0420, 0x0405),  # Р + Ѕ
        (0x0420, 0x00B0),  # Р + °
        (0x0420, 0x00B5),  # Р + µ
        (0x0420, 0x0455),  # Р + ѕ
        (0x0420, 0x0451),  # Р + ё
        (0x0421, 0x0402),  # С + Ђ
        (0x0421, 0x0401),  # С + Ѓ
        (0x0420, 0x0491),  # Р + ґ
        (0x0421, 0x2021),  # С + ‡
        (0x0420, 0x2013),  # Р + –
        (0x0420, 0x201C),  # Р + “
        (0x0420, 0x0406),  # Р + І
    )
)


def main() -> int:
    problems = []
    for path in sorted(pathlib.Path(".").rglob("*.py")):
        if any(part in {".git", "venv", "build", "dist", "__pycache__"} for part in path.parts):
            continue
        data = path.read_bytes()
        if data.startswith(b"\xef\xbb\xbf"):
            problems.append(f"{path}: файл начинается с BOM")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            problems.append(f"{path}: не UTF-8 ({error})")
            continue
        if "\ufffd" in text:
            problems.append(f"{path}: содержит символ замены U+FFFD")
            continue
        for marker in MARKERS:
            if marker in text:
                problems.append(f"{path}: похоже на кракозябры ({marker!r})")
                break

    if problems:
        print("Ошибка кодировки исходников:", file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print("Кодировка исходников в порядке.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
