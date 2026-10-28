"""Оркестрация первого запуска: что ставить, в каком порядке и как показать.

Сборка «lite» содержит только сам интерфейс. Всё, что нужно для скачивания
видео, при первом запуске автоматически кладётся в
``%LOCALAPPDATA%\\Media-Downloader\\tools``:

* yt-dlp, yt-dlp-ejs и bgutil-плагин — из wheel-архивов PyPI (без pip);
* FFmpeg и ffprobe — сборка с GitHub;
* Deno, Node.js и MinGit — портативные zip-архивы, без установщиков и прав
  администратора;
* PO Token provider — собирается локально из Node.js и Git по требованию.

Сами загрузки и проверки живут в ``components.py``; здесь — выбор компонентов,
параллельный запуск, общий прогресс и окно первого запуска.
"""

import logging
import os
import sys
import threading
from pathlib import Path

from components import (
    component_status,
    deno_ready,
    ffmpeg_ready,
    git_ready,
    install_deno,
    install_ffmpeg,
    install_git,
    install_node,
    install_po_provider,
    install_ytdlp,
    installed_ytdlp_version,
    node_ready,
    pot_ready,
    prepare_imports,
    ytdlp_ready,
)

LOGGER = logging.getLogger("media_downloader.bootstrap")

STAGE_WEIGHTS = {
    "yt-dlp": 25,
    "ffmpeg": 25,
    "deno": 15,
    "node": 15,
    "git": 10,
    "po": 10,
}


def _emit(log):
    return log or (lambda _message: None)


def _report(progress):
    return progress or (lambda _fraction: None)


# --------------------------------------------------------------------------
# общий запуск
# --------------------------------------------------------------------------

def missing_minimum() -> list[str]:
    missing = []
    if not ytdlp_ready():
        missing.append("yt-dlp")
    if not ffmpeg_ready():
        missing.append("ffmpeg")
    return missing


def ensure_minimum(log=None, progress=None) -> list[str]:
    """Ставит только то, без чего нельзя скачивать: yt-dlp и FFmpeg.

    Компоненты скачиваются параллельно, чтобы первый запуск не ждал
    последовательно два больших архива.
    """
    emit = _emit(log)
    report = _report(progress)
    errors: list[str] = []
    tasks = []
    if not ytdlp_ready():
        tasks.append(("yt-dlp", 30, lambda stage: install_ytdlp(log=emit, progress=stage)))
    if not ffmpeg_ready():
        tasks.append(("ffmpeg", 70, lambda stage: install_ffmpeg(log=emit, progress=stage)))
    if not tasks:
        return errors

    if len(tasks) == 1:
        name, _weight, action = tasks[0]
        try:
            action(report)
        except Exception as error:
            LOGGER.exception("Could not install component: %s", name)
            errors.append(f"{name}: {error}")
        return errors

    total = sum(weight for _name, weight, _action in tasks)
    fractions = {name: 0.0 for name, _weight, _action in tasks}
    lock = threading.Lock()
    last_reported = [-1.0]

    def stage_for(name: str):
        def update(fraction):
            value = max(0.0, min(float(fraction), 1.0))
            with lock:
                fractions[name] = value
                overall = sum(fractions[n] * weight for n, weight, _a in tasks) / total
                if overall < 1.0 and overall - last_reported[0] < 0.005:
                    return
                last_reported[0] = overall
            report(overall)
        return update

    def run(name: str, weight: int, action):
        try:
            action(stage_for(name))
        except Exception as error:
            LOGGER.exception("Could not install component: %s", name)
            with lock:
                errors.append(f"{name}: {error}")

    threads = []
    # FFmpeg качается дольше — запускаем его первым.
    for name, weight, action in sorted(tasks, key=lambda item: item[1], reverse=True):
        worker = threading.Thread(target=run, args=(name, weight, action),
                                  name=f"Bootstrap-{name}", daemon=True)
        threads.append(worker)
        worker.start()
    for worker in threads:
        worker.join()
    report(1.0)
    return errors


def ensure_all(log=None, progress=None) -> list[str]:
    """Ставит всё, включая тяжёлые компоненты (Deno, Node.js, Git, PO Token)."""
    emit = _emit(log)
    report = _report(progress)
    errors: list[str] = []
    minimum_weight = STAGE_WEIGHTS["yt-dlp"] + STAGE_WEIGHTS["ffmpeg"]
    total = sum(weight for _name, weight in STAGE_WEIGHTS.items()) or 1

    def minimum_stage(fraction):
        report(minimum_weight * max(0.0, min(fraction, 1.0)) / total)

    errors.extend(ensure_minimum(log=emit, progress=minimum_stage))
    done_weight = minimum_weight
    stages = [
        ("deno", lambda stage: install_deno(log=emit, progress=stage)),
        ("node", lambda stage: install_node(log=emit, progress=stage)),
        ("git", lambda stage: install_git(log=emit, progress=stage)),
        ("po", lambda stage: _install_po_stage(emit, stage)),
    ]
    for name, action in stages:
        weight = STAGE_WEIGHTS.get(name, 10)

        def stage(fraction, done=done_weight, weight=weight):
            report((done + weight * max(0.0, min(fraction, 1.0))) / total)

        try:
            action(stage)
        except Exception as error:
            LOGGER.exception("Could not install component: %s", name)
            errors.append(f"{name}: {error}")
        done_weight += weight
        report(done_weight / total)
    return errors


def _install_po_stage(log, stage) -> None:
    ok, message = install_po_provider(log=log, progress=stage)
    if not ok:
        raise RuntimeError(message)


# --------------------------------------------------------------------------
# окно прогресса первого запуска
# --------------------------------------------------------------------------

class _ProgressWindow:
    """Простое окно на чистом tkinter (ttkbootstrap ещё не импортирован)."""

    def __init__(self, title: str, icon: str | None = None):
        import queue
        import tkinter as tk
        from tkinter import ttk

        self._queue = queue.Queue()
        self._errors: list[str] = []
        self._root = tk.Tk()
        self._root.title(title)
        self._root.resizable(False, False)
        self._root.configure(bg="#20242a")
        if icon and os.path.exists(icon):
            try:
                self._root.iconbitmap(icon)
            except Exception:
                pass
        width, height = 460, 220
        screen_w, screen_h = self._root.winfo_screenwidth(), self._root.winfo_screenheight()
        self._root.geometry(f"{width}x{height}+{(screen_w - width) // 2}+{(screen_h - height) // 3}")

        frame = tk.Frame(self._root, bg="#20242a", padx=22, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text="Media Downloader",
            bg="#20242a",
            fg="#f5f5f5",
            font=("Segoe UI Semibold", 15),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            frame,
            text="Первый запуск: устанавливаю компоненты, это займёт пару минут.",
            bg="#20242a",
            fg="#b9c0c9",
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=410,
        ).pack(fill="x", pady=(4, 12))
        self._bar = ttk.Progressbar(frame, mode="determinate", maximum=100, value=0)
        self._bar.pack(fill="x")
        self._label = tk.Label(
            frame,
            text="Проверяю компоненты...",
            bg="#20242a",
            fg="#d7dde4",
            font=("Segoe UI", 9),
            anchor="w",
            justify="left",
            wraplength=410,
        )
        self._label.pack(fill="x", pady=(10, 0))
        tk.Label(
            frame,
            text="Окно можно свернуть — установка продолжится.",
            bg="#20242a",
            fg="#8c96a1",
            font=("Segoe UI", 8),
            anchor="w",
        ).pack(fill="x", pady=(6, 0))
        self._done = False
        # Закрытие крестиком не должно обрывать установку: сворачиваем окно,
        # иначе startup() увидит «компоненты не установлены» и завершит программу.
        self._root.protocol("WM_DELETE_WINDOW", self._root.iconify)
        self._root.update_idletasks()

    def report(self, message: str, fraction: float | None = None) -> None:
        self._queue.put((message, fraction))

    def run(self, worker) -> list[str]:
        self._worker_thread = threading.Thread(target=self._work, args=(worker,), daemon=True)
        self._worker_thread.start()
        self._root.after(80, self._poll)
        self._root.mainloop()
        # Если окно всё же закрыли другим способом, даём установке завершиться,
        # а не выходим с ложной ошибкой.
        self._worker_thread.join()
        return self._errors

    def _work(self, worker):
        try:
            worker(self.report)
        finally:
            self._queue.put(("__done__", None))

    def _poll(self):
        try:
            while True:
                message, fraction = self._queue.get_nowait()
                if message == "__done__":
                    self._done = True
                    continue
                if message:
                    self._label.config(text=message)
                if fraction is not None:
                    self._bar["value"] = max(0.0, min(fraction * 100.0, 100.0))
        except Exception:
            pass
        if self._done:
            self._root.after(250, self._root.destroy)
            return
        self._root.after(80, self._poll)


def startup(log=None) -> bool:
    """Проверяет минимум и при необходимости показывает окно установки.

    Возвращает False, если компоненты установить не удалось — тогда запускать
    интерфейс бессмысленно.
    """
    prepare_imports()
    missing = missing_minimum()
    if not missing:
        return True

    emit = _emit(log)
    LOGGER.info("First run bootstrap required: %s", missing)

    def worker(report):
        errors = ensure_minimum(
            log=lambda message: report(message),
            progress=lambda fraction: report("", fraction),
        )
        if errors:
            window._errors.extend(errors)

    window = None
    try:
        icon = str(Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "icon.ico")
        window = _ProgressWindow("Media Downloader — подготовка", icon)
        errors = window.run(worker)
    except Exception:
        LOGGER.exception("Progress window unavailable; installing without UI")
        errors = ensure_minimum(log=emit)

    still_missing = missing_minimum()
    if errors or still_missing:
        LOGGER.error("Bootstrap failed: errors=%s missing=%s", errors, still_missing)
        details = list(errors)
        if still_missing:
            details.append("Не установлено: " + ", ".join(still_missing))
        _show_error(
            "Не удалось установить компоненты:\n\n"
            + "\n".join(details)
            + "\n\nПроверьте подключение к интернету и запустите программу снова."
        )
        return False
    LOGGER.info("Bootstrap finished successfully")
    return True


def _show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Media Downloader", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)
