"""Самопроверка собранного .exe: ``Media-Downloader.exe --selftest``.

Проверяет то, что чаще всего ломается при сборке: версию и файлы рядом с
программой, импорт модулей, папку компонентов и доступность Tk. Сеть и
установка компонентов не нужны, поэтому проверку можно запускать в CI сразу
после сборки.

Отчёт печатается в stdout и сохраняется рядом с компонентами
(``%LOCALAPPDATA%\\Media-Downloader\\selftest.txt``): у оконной сборки нет
консоли, и файл — единственный способ увидеть результат.
"""

import importlib
import sys
import tkinter as tk
from pathlib import Path

import components
import utils

REPORT_NAME = "selftest.txt"
APP_MODULES = ("utils", "net", "diagnostics", "pot", "ffmpeg_tools", "components", "bootstrap")
OK = "OK"
ERROR = "ОШИБКА"
INFO = "ИНФО"


def _bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).parent))


def _check_version() -> str:
    version = utils.get_app_version().strip()
    if not version or version == "0.0.0":
        raise RuntimeError("версия не найдена в version.txt")
    return version


def _check_bundle_files() -> str:
    missing = [name for name in ("version.txt", "icon.ico") if not (_bundle_dir() / name).is_file()]
    if missing:
        raise RuntimeError("нет файлов: " + ", ".join(missing))
    return "version.txt, icon.ico"


def _check_modules() -> str:
    for name in APP_MODULES:
        importlib.import_module(name)
    return ", ".join(APP_MODULES)


def _check_ui_library() -> str:
    ttkbootstrap = importlib.import_module("ttkbootstrap")
    return f"ttkbootstrap {getattr(ttkbootstrap, '__version__', '')}".strip()


def _check_components_dir() -> str:
    root = utils.data_root()
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".selftest-write"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return str(root)


def _check_tk() -> str:
    root = tk.Tk()
    try:
        root.withdraw()
        root.update_idletasks()
    finally:
        root.destroy()
    return f"Tk {tk.TkVersion}"


def _component_summary() -> str:
    parts = []
    if components.ytdlp_ready():
        parts.append("yt-dlp " + (components.installed_ytdlp_version() or "готов"))
    else:
        parts.append("yt-dlp не установлен")
    parts.append("FFmpeg готов" if components.ffmpeg_ready() else "FFmpeg не установлен")
    return "; ".join(parts)


def _run_checks() -> list[tuple[str, str, str]]:
    checks = [
        ("Версия приложения", _check_version),
        ("Файлы сборки", _check_bundle_files),
        ("Модули приложения", _check_modules),
        ("Библиотека интерфейса", _check_ui_library),
        ("Папка компонентов", _check_components_dir),
        ("Tkinter", _check_tk),
    ]
    results = []
    for name, action in checks:
        try:
            results.append((OK, name, str(action())))
        except Exception as error:
            results.append((ERROR, name, f"{type(error).__name__}: {error}"))
    mode = "собранный .exe" if getattr(sys, "frozen", False) else "запуск из исходников"
    results.insert(0, (INFO, "Режим", mode))
    try:
        results.append((INFO, "Компоненты", _component_summary()))
    except Exception as error:
        results.append((INFO, "Компоненты", f"не удалось проверить: {error}"))
    return results


def format_report(results: list[tuple[str, str, str]]) -> str:
    lines = ["Media Downloader — самопроверка", ""]
    for status, name, detail in results:
        line = f"[{status}] {name}"
        if detail:
            line += f": {detail}"
        lines.append(line)
    failed = sum(1 for status, _name, _detail in results if status == ERROR)
    lines.append("")
    lines.append("Итог: всё в порядке." if not failed else f"Итог: ошибок — {failed}.")
    return "\n".join(lines)


def _report_path() -> Path:
    return utils.data_root() / REPORT_NAME


def run(write_file: bool = True) -> tuple[bool, str]:
    """Выполняет проверки. Возвращает (успех, текст отчёта)."""
    results = _run_checks()
    text = format_report(results)
    ok = not any(status == ERROR for status, _name, _detail in results)
    if write_file:
        try:
            path = _report_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text + "\n", encoding="utf-8")
            text += f"\n\nОтчёт сохранён: {path}"
        except OSError as error:
            text += f"\n\nНе удалось сохранить отчёт: {error}"
    return ok, text


def main() -> int:
    ok, text = run()
    print(text)
    return 0 if ok else 1
