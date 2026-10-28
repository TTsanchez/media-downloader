import json
import logging
import queue
import os
import re
import sys
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import bootstrap

# До импорта downloader/yt_dlp: обновлённый yt-dlp из tools\pylibs важнее встроенного.
bootstrap.prepare_imports()

from diagnostics import (get_diagnostics, initialize_logging, redact, register_secret,
                         report_exception, run_process)

if __name__ == "__main__":
    initialize_logging(notify_fatal=True)
    if not bootstrap.startup():
        sys.exit(1)

LOGGER = logging.getLogger("media_downloader.ui")

import tkinter as tk
from tkinter import filedialog, messagebox

import ttkbootstrap as ttkb
from ttkbootstrap.constants import BOTH, HORIZONTAL, LEFT, RIGHT, VERTICAL, X, Y

try:
    from ttkbootstrap.tooltip import ToolTip
except ImportError:
    from ttkbootstrap import ToolTip


def add_tooltip(widget, text: str, **kwargs):
    try:
        return ToolTip(widget, text=text, **kwargs)
    except TypeError:
        return ToolTip(widget, text, **kwargs)


def resource_path(name: str) -> str:
    """Путь к ресурсу и в режиме исходников, и внутри собранного .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)

from downloader import DownloadProgress, MediaDownloader
from pot import (
    SERVER_HOST,
    SERVER_PORT,
    provider_installed,
    server_running,
    start_server,
    stop_server,
)
from utils import (
    browser_reads_ok,
    check_cookies_file,
    check_youtube_network,
    detect_browsers,
    find_ffmpeg,
    find_ffprobe,
    format_bytes,
    format_duration,
    format_eta,
    get_app_asset,
    get_app_version,
    get_default_download_dir,
    get_latest_app_release,
    get_latest_pypi_version,
    is_browser_running,
    is_newer_app_version,
    is_newer_version,
    looks_like_url,
    tools_root,
    download_file,
    create_updater_script,
)

CONFIG_FILE = Path.home() / ".media_downloader_config.json"
LEGACY_CONFIG_FILE = Path.home() / ".yt_downloader_config.json"
FFMPEG_URL = "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest"
RELEASES_URL = "https://github.com/TTsanchez/media-downloader/releases/latest"
DEFAULT_ARCHIVE_FILE = Path.home() / ".media_downloader_archive.txt"
QUALITY_PRESETS = ("Авто", "2160p", "1440p", "1080p", "720p", "480p")
QUEUE_CONFIG_KEY = "queue"
MAX_SAVED_QUEUE = 500

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class VScroll(ttkb.Frame):
    """Фрейм с вертикальной прокруткой (колесо мыши + полоса справа)."""

    def __init__(self, master, height=250, bg=None, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0, bd=0, takefocus=0)
        if bg:
            self.canvas.configure(bg=bg)
        self.canvas.pack(side=LEFT, fill=BOTH, expand=True)

        sb = ttkb.Scrollbar(self, orient=VERTICAL, command=self.canvas.yview, bootstyle="round")
        sb.pack(side=RIGHT, fill=Y)
        self.canvas.configure(yscrollcommand=sb.set)

        self.inner = ttkb.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _on_wheel(self, event):
        # Крутим только если курсор внутри этой области (иначе не мешаем журналу)
        node = event.widget
        while node is not None:
            if node is self.canvas:
                self.canvas.yview_scroll(int(-event.delta / 120), "units")
                return
            node = getattr(node, "master", None)


class YTApp(ttkb.Window):
    def __init__(self):
        super().__init__(title="Media Downloader", size=(1000, 880), minsize=(900, 780))
        self._ui_thread = threading.get_ident()
        self._ui_events = queue.Queue()
        self._closing = False
        self._info_loading = False
        self._info_downloader = None
        self._updating = False
        self._installing_ffmpeg = False
        self._installing_components = False
        self._exporting_report = False
        self._last_error_notice = 0.0
        self._log_storage_warning_shown = False
        self.after(40, self._drain_ui_events)
        self._fit_to_screen()
        try:
            ico = resource_path("icon.ico")
            if os.path.exists(ico):
                self.iconbitmap(ico)
        except Exception:
            LOGGER.debug("Optional operation failed: __init__", exc_info=True)
        self._set_theme()
        self.downloader = MediaDownloader(progress_callback=self.on_progress)
        self.cookies_path: str | None = None
        self.is_downloading = False
        self.config = self._load_config()

        self._init_vars()
        self._build_ui()
        self._apply_config()
        self._restore_queue()
        self._ui_ready = True
        self._restore_geometry()
        self._check_ffmpeg()
        self._refresh_browser_list()
        saved = getattr(self, "_saved_browser", "без cookies")
        if saved and saved != "без cookies" and browser_reads_ok(saved):
            for label, key in self._browser_by_label.items():
                if key == saved:
                    self.cmb_browser.set(label)
                    break
        self._refresh_browser_status()
        self.update_idletasks()
        self._save_config()
        self._check_components()
        threading.Thread(target=self._autostart_pot, daemon=True).start()
        self._setup_shortcuts()
        self.url_var.trace_add("write", self._on_url_change)
        self.bind("<FocusIn>", self._on_focus_in)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log("Готово. Вставьте ссылку с YouTube и нажмите «Получить информацию»")
        self._check_updates_async()
        diagnostic = get_diagnostics()
        if diagnostic:
            self._log(f"Диагностический журнал: {diagnostic.log_file or 'в памяти (сохраните отчёт до закрытия)'}")
            tools = dict(diagnostic.context.get("ffmpeg", {}))
            tools["deno"] = diagnostic.context.get("youtube_components", {}).get("deno")
            threading.Thread(target=self._probe_component_versions, args=(tools,),
                             name="Component-versions", daemon=True).start()

    def _probe_component_versions(self, tools):
        versions = {}
        for name, path in tools.items():
            if not path:
                versions[name] = "not found"
                continue
            code, output = run_process([path, "--version" if name == "deno" else "-version"], timeout=5)
            versions[name] = output.splitlines()[0] if output.strip() and not code else f"unavailable (exit={code})"
        diagnostic = get_diagnostics()
        if diagnostic:
            diagnostic.update_context("component_versions", versions)
        LOGGER.info("Component versions collected: %s", versions)

    def _post_ui(self, callback):
        """Передаёт результат фоновой работы в основной поток Tk."""
        if not self._closing:
            self._ui_events.put(callback)

    def _drain_ui_events(self):
        if self._closing:
            return
        for _ in range(100):
            try:
                callback = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                self.report_callback_exception(*sys.exc_info())
        self._show_pending_errors()
        self.after(40, self._drain_ui_events)

    def report_callback_exception(self, exc, value, traceback):
        report_exception("Ошибка обработчика интерфейса", (exc, value, traceback))

    def _show_pending_errors(self):
        diagnostic = get_diagnostics()
        if not diagnostic or not getattr(self, "_ui_ready", False):
            return
        storage_error = getattr(diagnostic.handler, "storage_error", None)
        if storage_error and not self._log_storage_warning_shown:
            self._log_storage_warning_shown = True
            messagebox.showwarning("Журнал диагностики", storage_error)
        try:
            title = diagnostic.notifications.get_nowait()
        except queue.Empty:
            return
        self.status_var.set("Обнаружена ошибка — сохраните отчёт")
        self._log(title)
        now = time.monotonic()
        if not self._last_error_notice or now - self._last_error_notice >= 10:
            self._last_error_notice = now
            messagebox.showerror("Ошибка приложения", f"{title}.\n\nПодробности записаны в журнал. "
                                 "Нажмите «Сохранить отчёт» и приложите архив к сообщению об ошибке.")

    def _playlist_range(self):
        if self.playlist_all_var.get():
            return 1, 0
        try:
            start, end = self.playlist_start_var.get(), self.playlist_end_var.get()
            if start < 1 or end < 0 or (end and end < start):
                raise ValueError
        except (ValueError, tk.TclError):
            raise ValueError("Укажите целые номера: начало от 1, конец не меньше начала или 0 — до конца.") from None
        return start, end

    def _on_url_change(self, *_args):
        if self._paused_download and not self.is_downloading:
            self._paused_download = False
            self._last_download_args = None
            try:
                self.btn_download.configure(text="Скачать")
            except Exception:
                LOGGER.debug("Optional operation failed: _on_url_change", exc_info=True)
        self._populate_formats({})
        self.title_lbl.config(text="Название: —")
        self.meta_lbl.config(text="Длительность: —    Канал: —")

    def _fit_to_screen(self):
        """Подгоняет окно под экран, чтобы нижние кнопки не уезжали за край."""
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        except Exception:
            return
        w = min(1040, max(900, sw - 60))
        h = min(900, max(620, sh - 70))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 3)
        self.minsize(900, min(780, h))
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _restore_geometry(self):
        """Восстанавливает размер и положение окна из прошлого запуска."""
        geo = (self.config.get("geometry") or "").strip()
        if not geo:
            return
        m = re.fullmatch(r"(\d+)x(\d+)([+-]\d+)([+-]\d+)", geo)
        if not m:
            return
        try:
            w, h, x, y = (int(m.group(i)) for i in range(1, 5))
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            w = min(max(w, 900), max(900, sw - 40))
            h = min(max(h, 620), max(620, sh - 70))
            x = max(0, min(x, sw - w))
            y = max(0, min(y, sh - h - 40))
            self.minsize(900, min(780, h))
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            LOGGER.warning("Could not restore window geometry", exc_info=True)

    def _set_theme(self):
        for name in ("bootstrap-dark", "darkly", "cyborg", "superhero"):
            try:
                self.style.theme_use(name)
                break
            except Exception:
                continue
        self.option_add("*Font", "{Segoe UI} 10")
        self.style.configure("Treeview", rowheight=28, font=("Segoe UI", 9))
        self.style.configure("Treeview.Heading", font=("Segoe UI Semibold", 9))
        self.style.configure("TNotebook.Tab", padding=(18, 9))

    # ---------- config ----------

    def _load_config(self) -> dict:
        try:
            path = CONFIG_FILE if CONFIG_FILE.exists() else LEGACY_CONFIG_FILE
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            LOGGER.warning("Could not load configuration; using defaults", exc_info=True)
        return {}

    def _save_config(self):
        if not getattr(self, "_ui_ready", False):
            return
        try:
            register_secret(self.proxy_var.get().strip())
            data = {
                "output_dir": self.output_dir_var.get(),
                "audio_format": self.audio_format_var.get(),
                "audio_quality": self.audio_quality_var.get(),
                "restrict_filenames": self.restrict_names_var.get(),
                "skip_downloaded": self.skip_downloaded_var.get(),
                "archive_file": self.archive_var.get().strip(),
                "browser": self._active_browser() or "без cookies",
                "cookies_file": self.cookies_path or "",
                "proxy": self.proxy_var.get().strip(),
                "geometry": self.winfo_geometry(),
                QUEUE_CONFIG_KEY: self._queue_snapshot(),
            }
            temporary = CONFIG_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            temporary.replace(CONFIG_FILE)
        except Exception:
            LOGGER.error("Could not save configuration", exc_info=True)

    def _apply_config(self):
        self.output_dir_var.set(self.config.get("output_dir", str(get_default_download_dir())))
        self.audio_format_var.set(self.config.get("audio_format", "mp3"))
        self.audio_quality_var.set(self.config.get("audio_quality", "192"))
        self.restrict_names_var.set(self.config.get("restrict_filenames", True))
        self.skip_downloaded_var.set(bool(self.config.get("skip_downloaded", False)))
        self.archive_var.set(self.config.get("archive_file") or str(DEFAULT_ARCHIVE_FILE))
        self._saved_browser = self.config.get("browser", "без cookies")
        self.proxy_var.set(self.config.get("proxy", ""))
        register_secret(self.proxy_var.get().strip())

        # Восстанавливаем cookies.txt: для обычных публичных видео они не нужны
        # (yt-dlp идёт анонимным клиентом), но нужны для возрастных/приватных видео
        # и для «подтвердите, что вы не робот».
        saved_cookies = self.config.get("cookies_file", "")
        if saved_cookies and Path(saved_cookies).is_file():
            self.cookies_path = saved_cookies
            self._apply_cookie_status(saved_cookies)
        else:
            self.cookies_path = None

    def _queue_snapshot(self) -> list[dict]:
        """Возвращает очередь для сохранения в конфиге (без путей и состояния потока)."""
        snapshot = []
        for item in getattr(self, "queue_items", [])[:MAX_SAVED_QUEUE]:
            if item.get("status") == "готово":
                continue
            snapshot.append({
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "mode": item.get("mode", "best"),
                "custom_format": item.get("custom_format"),
                "audio_only": bool(item.get("audio_only", False)),
                "audio_format": item.get("audio_format", "mp3"),
                "audio_quality": item.get("audio_quality", "192"),
                "start": item.get("start", 1),
                "end": item.get("end", 0),
                "fmt_label": item.get("fmt_label", ""),
            })
        return snapshot

    def _restore_queue(self):
        """Возвращает недокачанные ссылки из прошлого запуска."""
        entries = self.config.get(QUEUE_CONFIG_KEY) or []
        if not isinstance(entries, list):
            return
        restored = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url") or "").strip()
            if not looks_like_url(url) or any(it["url"] == url for it in self.queue_items):
                continue
            try:
                start = int(entry.get("start") or 1)
                end = int(entry.get("end") or 0)
            except (TypeError, ValueError):
                start, end = 1, 0
            self._queue_insert({
                "url": url,
                "title": str(entry.get("title") or url),
                "status": "ожидание",
                "percent": 0.0,
                "error": "",
                "mode": str(entry.get("mode") or "best"),
                "custom_format": entry.get("custom_format") or None,
                "audio_only": bool(entry.get("audio_only", False)),
                "audio_format": str(entry.get("audio_format") or "mp3"),
                "audio_quality": str(entry.get("audio_quality") or "192"),
                "start": max(1, start),
                "end": max(0, end),
                "fmt_label": str(entry.get("fmt_label") or "Лучшее"),
            })
            restored += 1
        if restored:
            self._log(f"Восстановлена очередь: {restored} ссылок")

    def _init_vars(self):
        v = tk.StringVar
        self.url_var = v()
        self.output_dir_var = v()
        self.format_var = v(value="best")
        self.audio_format_var = v(value="mp3")
        self.audio_quality_var = v(value="192")
        self.restrict_names_var = tk.BooleanVar(value=True)
        self.skip_downloaded_var = tk.BooleanVar(value=False)
        self.archive_var = v(value=str(DEFAULT_ARCHIVE_FILE))
        self.browser_var = v(value="без cookies")
        self.proxy_var = v()
        self._browser_by_label: dict[str, str | None] = {"без cookies": None}
        self._detected: list[tuple[str, str]] = []
        self.playlist_all_var = tk.BooleanVar(value=True)
        self.playlist_start_var = tk.IntVar(value=1)
        self.playlist_end_var = tk.IntVar(value=0)
        self.status_var = v(value="Готов к работе")
        self.speed_var = v(value="—")
        self.eta_var = v(value="—")
        self.size_var = v(value="—")
        self.preset_var = v(value=QUALITY_PRESETS[0])
        self._last_clip = ""
        self._ui_ready = False

        # формат-таблица
        self._fmt_map: dict[str, tuple[str, str]] = {}
        self._selected_format: str | None = None
        self._selected_audio_only = False
        self._selected_label = ""
        self._current_info: dict = {}

        # пауза одиночной загрузки
        self._paused_download = False
        self._last_download_args: dict | None = None

        # очередь
        self.q_parallel_var = tk.IntVar(value=1)
        self.queue_items: list[dict] = []
        self._q_lock = threading.Lock()
        self._q_running = False
        self._q_paused = False
        self._q_stop = threading.Event()
        self._q_active: dict[int, MediaDownloader] = {}
        self._q_done_count = 0
        self._q_error_count = 0

    # ---------- ui ----------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        try:
            bg = self.style.lookup("TFrame", "background") or "#2b3035"
        except Exception:
            bg = "#2b3035"

        header = ttkb.Frame(self, padding=(20, 10, 20, 8))
        header.grid(row=0, column=0, sticky="ew")
        brand = ttkb.Frame(header)
        brand.pack(side=LEFT)
        ttkb.Label(brand, text="Media Downloader", font=("Segoe UI Semibold", 20)).pack(anchor="w")
        ttkb.Label(brand, text="видео · аудио · плейлисты", font=("Segoe UI", 9), bootstyle="secondary").pack(
            anchor="w", pady=(2, 0)
        )
        self.ffmpeg_badge = ttkb.Label(header, text="FFmpeg: проверка...", font=("Segoe UI", 8), bootstyle="warning")
        self.ffmpeg_badge.pack(side=RIGHT)

        ttkb.Separator(self).grid(row=1, column=0, sticky="ew", padx=20)

        url_box = ttkb.LabelFrame(self, text=" Ссылка ", padding=(14, 10, 14, 10))
        url_box.grid(row=2, column=0, sticky="ew", padx=20, pady=(10, 8))
        row = ttkb.Frame(url_box)
        row.pack(fill=X)
        self.url_entry = ttkb.Entry(row, textvariable=self.url_var, font=("Segoe UI", 10))
        self.url_entry.pack(side=LEFT, fill=X, expand=True, ipady=4)
        self.url_entry.bind("<Return>", lambda e: self._get_info_async())
        ttkb.Button(row, text="Вставить", width=10, bootstyle="light-outline", command=self._paste_url).pack(side=LEFT, padx=(8, 0))
        ttkb.Button(row, text="Очистить", width=10, bootstyle="light-outline", command=self._clear_url).pack(side=LEFT, padx=(6, 0))
        self.btn_info = ttkb.Button(row, text="Получить информацию", width=24, bootstyle="primary", command=self._get_info_async)
        self.btn_info.pack(side=LEFT, padx=(6, 0))

        self.title_lbl = ttkb.Label(url_box, text="Название: —", font=("Segoe UI", 9, "bold"), wraplength=860, justify=LEFT)
        self.title_lbl.pack(anchor="w", fill=X, pady=(10, 0))
        self.meta_lbl = ttkb.Label(url_box, text="Длительность: —    Канал: —", font=("Segoe UI", 8), bootstyle="secondary", wraplength=860, justify=LEFT)
        self.meta_lbl.pack(anchor="w", fill=X, pady=(2, 0))

        self.nb = nb = ttkb.Notebook(self, padding=(0, 2))
        nb.grid(row=3, column=0, sticky="nsew", padx=20)

        tab_fmt = ttkb.Frame(nb)
        nb.add(tab_fmt, text="Формат")
        fmt_scroll = VScroll(tab_fmt, height=260, bg=bg)
        fmt_scroll.pack(fill=BOTH, expand=True)
        tab_fmt = ttkb.Frame(fmt_scroll.inner, padding=(16, 8, 16, 8))
        tab_fmt.pack(fill=BOTH, expand=True)
        fbox = ttkb.LabelFrame(tab_fmt, text=" Что скачивать ", padding=(14, 8, 14, 8))
        fbox.pack(fill=X)
        self.rb_best = ttkb.Radiobutton(fbox, text="Лучшее качество — видео + аудио (MP4)", variable=self.format_var, value="best", command=self._on_mode_change)
        self.rb_best.pack(anchor="w", pady=1)
        add_tooltip(self.rb_best, "Скачает лучшее доступное видео и лучшую дорожку звука, затем склеит в один MP4")
        self.rb_video = ttkb.Radiobutton(fbox, text="Только видео (без звука)", variable=self.format_var, value="bestvideo", command=self._on_mode_change)
        self.rb_video.pack(anchor="w", pady=1)
        self.rb_audio = ttkb.Radiobutton(fbox, text="Только аудио", variable=self.format_var, value="bestaudio", command=self._on_mode_change)
        self.rb_audio.pack(anchor="w", pady=1)
        preset_row = ttkb.Frame(fbox)
        preset_row.pack(fill=X, pady=(6, 0))
        ttkb.Label(preset_row, text="Пресет качества:", width=16).pack(side=LEFT)
        self.cmb_preset = ttkb.Combobox(
            preset_row, values=QUALITY_PRESETS, textvariable=self.preset_var,
            state="readonly", width=12,
        )
        self.cmb_preset.pack(side=LEFT)
        self.cmb_preset.bind("<<ComboboxSelected>>", self._on_preset_change)
        ttkb.Label(
            preset_row,
            text="Ограничивает разрешение, не трогая остальные настройки",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        ).pack(side=LEFT, padx=(10, 0))

        self.audio_box = ttkb.LabelFrame(tab_fmt, text=" Настройки аудио ", padding=(14, 6, 14, 6))
        self.audio_box.pack(fill=X, pady=(6, 0))
        arow = ttkb.Frame(self.audio_box)
        arow.pack(fill=X)
        ttkb.Label(arow, text="Формат:", width=10).pack(side=LEFT)
        self.cmb_af = ttkb.Combobox(arow, values=["mp3", "m4a", "opus", "wav"], textvariable=self.audio_format_var, state="readonly", width=10)
        self.cmb_af.pack(side=LEFT, padx=(5, 0))
        self.cmb_af.bind("<<ComboboxSelected>>", lambda e: self._update_state())
        ttkb.Label(arow, text="Битрейт:", width=12).pack(side=LEFT, padx=(18, 0))
        self.cmb_aq = ttkb.Combobox(arow, values=["128", "160", "192", "256", "320"], textvariable=self.audio_quality_var, state="readonly", width=8)
        self.cmb_aq.pack(side=LEFT, padx=(5, 0))
        self.cmb_aq.bind("<<ComboboxSelected>>", lambda e: self._save_config())
        ttkb.Label(arow, text="kbps", bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(6, 0))
        ttkb.Label(self.audio_box, text="mp3 — универсальный · m4a — Apple · opus — лучшее сжатие · wav — без потерь", bootstyle="secondary", font=("Segoe UI", 8)).pack(
            anchor="w", pady=(6, 0)
        )

        tblbox = ttkb.LabelFrame(tab_fmt, text=" Доступные форматы ", padding=(10, 6, 10, 6))
        tblbox.pack(fill=BOTH, expand=True, pady=(6, 0))
        cols = ("quality", "type", "codec", "fps", "size", "note")
        self.fmt_tree = ttkb.Treeview(tblbox, columns=cols, show="headings", selectmode="browse", height=5, bootstyle="light")
        for cid, text, w, anchor in (
            ("quality", "Качество", 110, "w"),
            ("type", "Тип", 110, "w"),
            ("codec", "Кодек", 150, "w"),
            ("fps", "FPS", 50, "center"),
            ("size", "Размер", 100, "e"),
            ("note", "Примечание", 240, "w"),
        ):
            self.fmt_tree.heading(cid, text=text)
            self.fmt_tree.column(cid, width=w, anchor=anchor, stretch=(cid == "note"))
        self.fmt_tree.pack(side=LEFT, fill=BOTH, expand=True)
        fsb = ttkb.Scrollbar(tblbox, orient=VERTICAL, command=self.fmt_tree.yview)
        fsb.pack(side=RIGHT, fill=Y)
        self.fmt_tree.configure(yscrollcommand=fsb.set)
        self.fmt_tree.bind("<<TreeviewSelect>>", self._on_format_select)
        self.fmt_lbl = ttkb.Label(tab_fmt, text="Формат не выбран — используется выбор выше.", bootstyle="secondary", font=("Segoe UI", 8))
        self.fmt_lbl.pack(anchor="w", pady=(4, 0))

        tab_q = ttkb.Frame(nb)
        nb.add(tab_q, text="Очередь")
        queue_scroll = VScroll(tab_q, height=260, bg=bg)
        queue_scroll.pack(fill=BOTH, expand=True)
        tab_q = ttkb.Frame(queue_scroll.inner, padding=(16, 8, 16, 8))
        tab_q.pack(fill=BOTH, expand=True)

        qin = ttkb.LabelFrame(tab_q, text=" Ссылки — по одной в строке ", padding=(12, 8, 12, 8))
        qin.pack(fill=X)
        self.queue_input = tk.Text(
            qin, height=3, wrap="none", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white", borderwidth=0, padx=8, pady=6, relief="flat",
        )
        self.queue_input.pack(fill=X)
        qrow = ttkb.Frame(qin)
        qrow.pack(fill=X, pady=(6, 0))
        ttkb.Button(qrow, text="Добавить в очередь", width=20, bootstyle="primary", command=self._queue_add).pack(side=LEFT)
        ttkb.Button(qrow, text="Из файла...", width=14, bootstyle="light-outline", command=self._queue_add_from_file).pack(side=LEFT, padx=(8, 0))
        ttkb.Button(qrow, text="Вставить", width=12, bootstyle="light-outline", command=self._queue_paste).pack(side=LEFT, padx=(6, 0))
        ttkb.Label(qrow, text="Параллельно:", bootstyle="secondary", font=("Segoe UI", 8)).pack(side=RIGHT, padx=(0, 6))
        ttkb.Spinbox(qrow, from_=1, to=4, textvariable=self.q_parallel_var, width=4).pack(side=RIGHT)
        ttkb.Label(
            qin,
            text="Совет: перетащите .txt со ссылками на значок программы — очередь пополнится при запуске.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
        ).pack(anchor="w", pady=(6, 0))

        qlist = ttkb.LabelFrame(tab_q, text=" Очередь ", padding=(10, 6, 10, 6))
        qlist.pack(fill=BOTH, expand=True, pady=(6, 0))
        qcols = ("num", "title", "fmt", "status", "progress")
        self.queue_tree = ttkb.Treeview(qlist, columns=qcols, show="headings", height=5, bootstyle="light")
        for cid, text, w, anchor in (
            ("num", "№", 44, "center"),
            ("title", "Ссылка / название", 380, "w"),
            ("fmt", "Формат", 150, "w"),
            ("status", "Статус", 110, "center"),
            ("progress", "Прогресс", 90, "center"),
        ):
            self.queue_tree.heading(cid, text=text)
            self.queue_tree.column(cid, width=w, anchor=anchor, stretch=(cid == "title"))
        self.queue_tree.pack(side=LEFT, fill=BOTH, expand=True)
        qsb = ttkb.Scrollbar(qlist, orient=VERTICAL, command=self.queue_tree.yview)
        qsb.pack(side=RIGHT, fill=Y)
        self.queue_tree.configure(yscrollcommand=qsb.set)
        self.queue_tree.tag_configure("done", foreground="#66bb6a")
        self.queue_tree.tag_configure("error", foreground="#ef5350")
        self.queue_tree.tag_configure("working", foreground="#42a5f5")
        self.queue_tree.tag_configure("paused", foreground="#ffb74d")

        qctrl = ttkb.Frame(tab_q)
        qctrl.pack(fill=X, pady=(6, 0))
        self.btn_q_start = ttkb.Button(qctrl, text="Скачать всё", width=16, bootstyle="success", command=self._queue_start)
        self.btn_q_start.pack(side=LEFT)
        self.btn_q_pause = ttkb.Button(qctrl, text="Пауза", width=12, bootstyle="warning", command=self._queue_pause, state="disabled")
        self.btn_q_pause.pack(side=LEFT, padx=(8, 0))
        self.btn_q_stop = ttkb.Button(qctrl, text="Остановить", width=14, bootstyle="danger", command=self._queue_stop, state="disabled")
        self.btn_q_stop.pack(side=LEFT, padx=(8, 0))
        ttkb.Button(qctrl, text="Удалить выбранное", width=18, bootstyle="light-outline", command=self._queue_remove_selected).pack(side=LEFT, padx=(8, 0))
        ttkb.Button(qctrl, text="Очистить", width=12, bootstyle="light-outline", command=self._queue_clear).pack(side=LEFT, padx=(8, 0))

        tab_pl = ttkb.Frame(nb, padding=(16, 8, 16, 8))
        nb.add(tab_pl, text="Плейлист")
        pbox = ttkb.LabelFrame(tab_pl, text=" Диапазон ", padding=(14, 8, 14, 8))
        pbox.pack(fill=X)
        ttkb.Radiobutton(pbox, text="Весь плейлист", variable=self.playlist_all_var, value=True, command=self._update_state).pack(anchor="w", pady=1)
        ttkb.Radiobutton(pbox, text="Только выбранные видео", variable=self.playlist_all_var, value=False, command=self._update_state).pack(anchor="w", pady=1)
        prow = ttkb.Frame(pbox)
        prow.pack(fill=X, pady=(6, 0))
        ttkb.Label(prow, text="С номера:", width=12).pack(side=LEFT)
        self.sp_start = ttkb.Spinbox(prow, from_=1, to=99999, textvariable=self.playlist_start_var, width=9, state="disabled")
        self.sp_start.pack(side=LEFT, padx=(5, 0))
        ttkb.Label(prow, text="по номер:", width=11).pack(side=LEFT, padx=(14, 0))
        self.sp_end = ttkb.Spinbox(prow, from_=0, to=99999, textvariable=self.playlist_end_var, width=9, state="disabled")
        self.sp_end.pack(side=LEFT, padx=(5, 0))
        ttkb.Label(prow, text="(0 — до конца)", bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(6, 0))
        ttkb.Label(tab_pl, text="Для одиночного видео эти настройки игнорируются.", bootstyle="secondary", font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))

        tab_set = ttkb.Frame(nb)
        nb.add(tab_set, text="Настройки")
        _settings_scroll = VScroll(tab_set, height=150, bg=bg)
        _settings_scroll.pack(fill=BOTH, expand=True)
        tab_set = ttkb.Frame(_settings_scroll.inner, padding=(14, 10, 14, 10))
        tab_set.pack(fill=BOTH, expand=True)

        obox = ttkb.LabelFrame(tab_set, text="Папка сохранения", padding=(14, 10, 14, 10))
        obox.pack(fill=X)
        orow = ttkb.Frame(obox)
        orow.pack(fill=X)
        ttkb.Entry(orow, textvariable=self.output_dir_var).pack(side=LEFT, fill=X, expand=True, ipady=3)
        ttkb.Button(orow, text="Обзор...", width=11, bootstyle="light-outline", command=self._browse_dir).pack(side=LEFT, padx=(8, 0))
        ttkb.Button(orow, text="Открыть", width=11, bootstyle="light-outline", command=self._open_dir).pack(side=LEFT, padx=(6, 0))

        nbox = ttkb.LabelFrame(tab_set, text="Имена файлов", padding=(14, 10, 14, 10))
        nbox.pack(fill=X, pady=(10, 0))
        ttkb.Checkbutton(
            nbox,
            text="Ограничить недопустимые символы (рекомендуется)",
            variable=self.restrict_names_var,
            command=self._save_config,
        ).pack(anchor="w")

        dbox = ttkb.LabelFrame(tab_set, text="Архив загрузок — не качать повторно", padding=(14, 10, 14, 10))
        dbox.pack(fill=X, pady=(10, 0))
        ttkb.Checkbutton(
            dbox,
            text="Пропускать уже скачанное (ведётся список скачанных роликов)",
            variable=self.skip_downloaded_var,
            command=self._save_config,
        ).pack(anchor="w")
        darow = ttkb.Frame(dbox)
        darow.pack(fill=X, pady=(8, 0))
        ttkb.Entry(darow, textvariable=self.archive_var).pack(side=LEFT, fill=X, expand=True, ipady=3)
        ttkb.Button(darow, text="Обзор...", width=11, bootstyle="light-outline", command=self._browse_archive).pack(
            side=LEFT, padx=(8, 0)
        )
        ttkb.Label(
            dbox,
            text="Список хранится в отдельном файле. Повторный запуск тех же ссылок или плейлиста не скачает их заново.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 0))

        cbox = ttkb.LabelFrame(tab_set, text="Cookies — для возрастных и приватных видео", padding=(14, 10, 14, 10))
        cbox.pack(fill=X, pady=(10, 0))
        crow = ttkb.Frame(cbox)
        crow.pack(fill=X)
        self.cookie_lbl = ttkb.Label(crow, text="Cookies не загружены", bootstyle="secondary")
        self.cookie_lbl.pack(side=LEFT, fill=X, expand=True)
        ttkb.Button(crow, text="Загрузить", width=12, bootstyle="light-outline", command=self._load_cookies).pack(side=LEFT, padx=(6, 0))
        ttkb.Button(crow, text="Удалить", width=12, bootstyle="light-outline", command=self._clear_cookies).pack(side=LEFT, padx=(6, 0))
        ttkb.Label(
            cbox,
            text="Формат Netscape (.txt). Экспортируется расширениями Cookie-Editor или «Get cookies.txt LOCALLY». Файл хранится только на этом компьютере.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 0))

        bbox = ttkb.LabelFrame(cbox, text="Или взять cookies прямо из браузера", padding=(14, 10, 14, 10))
        bbox.pack(fill=X, pady=(10, 0))
        brow = ttkb.Frame(bbox)
        brow.pack(fill=X)
        ttkb.Label(brow, text="Браузер:", width=12).pack(side=LEFT)
        self.cmb_browser = ttkb.Combobox(
            brow,
            values=["без cookies"],
            textvariable=self.browser_var,
            state="readonly",
            width=16,
        )
        self.cmb_browser.pack(side=LEFT, padx=(5, 0))
        self.cmb_browser.bind("<<ComboboxSelected>>", lambda e: (self._refresh_browser_status(), self._save_config()))
        ttkb.Button(brow, text="Обновить", width=12, bootstyle="light-outline", command=self._refresh_browser_list).pack(side=LEFT, padx=(8, 0))
        ttkb.Button(brow, text="Сбросить", width=12, bootstyle="light-outline", command=self._reset_browser).pack(side=LEFT, padx=(6, 0))
        self.browser_status = ttkb.Label(
            bbox,
            text="",
            bootstyle="warning",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        )
        self.browser_status.pack(anchor="w", pady=(8, 0))
        ttkb.Label(
            bbox,
            text="Важно: браузер должен быть ПОЛНОСТЬЮ закрыт, иначе файл cookies заблокирован и будет ошибка «Could not copy cookie database».",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        ).pack(anchor="w", pady=(6, 0))

        cbox = ttkb.LabelFrame(tab_set, text="Компоненты — скачиваются автоматически", padding=(14, 10, 14, 10))
        cbox.pack(fill=X, pady=(10, 0))
        self.comp_labels: dict[str, ttkb.Label] = {}
        for key, title in (
            ("ytdlp", "yt-dlp"),
            ("ffmpeg", "FFmpeg"),
            ("deno", "Deno"),
            ("node", "Node.js"),
            ("git", "Git"),
            ("pot", "PO Token"),
        ):
            comp_row = ttkb.Frame(cbox)
            comp_row.pack(fill=X, pady=1)
            ttkb.Label(comp_row, text=f"{title}:", width=10).pack(side=LEFT)
            label = ttkb.Label(comp_row, text="проверка...", wraplength=700, justify=LEFT)
            label.pack(side=LEFT, fill=X, expand=True)
            self.comp_labels[key] = label

        crow = ttkb.Frame(cbox)
        crow.pack(fill=X, pady=(8, 0))
        ttkb.Button(crow, text="Проверить", width=13, bootstyle="light-outline",
                    command=self._check_components).pack(side=LEFT)
        self.btn_install_all = ttkb.Button(crow, text="Установить всё", width=20,
                                           bootstyle="success-outline", command=self._install_all_async)
        self.btn_install_all.pack(side=LEFT, padx=(8, 0))
        self.btn_ffmpeg = ttkb.Button(crow, text="Восстановить FFmpeg", width=24,
                                      bootstyle="light-outline", command=self._download_ffmpeg_async)
        self.btn_ffmpeg.pack(side=LEFT, padx=(8, 0))
        ttkb.Button(crow, text="Открыть папку", width=16, bootstyle="light-outline",
                    command=self._open_tools_dir).pack(side=LEFT, padx=(8, 0))
        ttkb.Label(
            cbox,
            text=(
                "yt-dlp и FFmpeg устанавливаются сами при первом запуске.\n"
                "Deno, Node.js, Git и PO Token нужны для защиты YouTube — их ставит кнопка «Установить всё».\n"
                f"Хранилище: {tools_root()} — права администратора не требуются."
            ),
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 0))

        netbox = ttkb.LabelFrame(tab_set, text="Сеть и прокси — обход блокировки YouTube", padding=(14, 10, 14, 10))
        netbox.pack(fill=X, pady=(10, 0))
        nrow = ttkb.Frame(netbox)
        nrow.pack(fill=X)
        ttkb.Label(nrow, text="Прокси:", width=10).pack(side=LEFT)
        ttkb.Entry(nrow, textvariable=self.proxy_var, font=("Segoe UI", 9)).pack(side=LEFT, fill=X, expand=True, ipady=3)
        ttkb.Button(nrow, text="Проверить сеть", width=16, bootstyle="light-outline", command=self._check_network_async).pack(
            side=LEFT, padx=(8, 0)
        )
        ttkb.Button(nrow, text="Сохранить", width=12, bootstyle="light-outline", command=self._save_config).pack(
            side=LEFT, padx=(6, 0)
        )
        self.net_lbl = ttkb.Label(
            netbox,
            text="Нажмите «Проверить сеть», чтобы узнать, доступен ли YouTube напрямую.",
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        )
        self.net_lbl.pack(anchor="w", pady=(8, 0))
        ttkb.Label(
            netbox,
            text=(
                "В России YouTube часто блокируется по имени домена (SNI/DPI): сайт не открывается, хотя интернет работает.\n"
                "Программа в этом случае качать не сможет — ни один локальный загрузчик не сможет. Помогает VPN или прокси.\n"
                "Формат прокси: http://хост:порт, socks5://хост:порт или http://логин:пароль@хост:порт. Пусто — прямое подключение."
            ),
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        ).pack(anchor="w", pady=(6, 0))

        ubox = ttkb.LabelFrame(tab_set, text="Обновление yt-dlp", padding=(14, 10, 14, 10))
        ubox.pack(fill=X, pady=(10, 0))
        self.ytdlp_lbl2 = ttkb.Label(ubox, text="yt-dlp: проверка...", wraplength=820, justify=LEFT)
        self.ytdlp_lbl2.pack(anchor="w")
        urow = ttkb.Frame(ubox)
        urow.pack(fill=X, pady=(8, 0))
        ttkb.Button(urow, text="Проверить обновления", width=20, bootstyle="light-outline", command=self._check_updates_async).pack(
            side=LEFT
        )
        ttkb.Button(urow, text="Обновить yt-dlp", width=18, bootstyle="light-outline", command=self._update_ytdlp_async).pack(
            side=LEFT, padx=(8, 0)
        )
        ttkb.Label(
            ubox,
            text=(
                "YouTube часто меняется, и свежий yt-dlp нередко решает ошибки загрузки.\n"
                "yt-dlp хранится в папке компонентов и обновляется здесь — переустанавливать программу не нужно."
            ),
            bootstyle="secondary",
            font=("Segoe UI", 8),
            wraplength=820,
            justify=LEFT,
        ).pack(anchor="w", pady=(8, 0))

        pbox2 = ttkb.LabelFrame(self, text=" Прогресс ", padding=(16, 8, 16, 8))
        pbox2.grid(row=4, column=0, sticky="ew", padx=20, pady=(8, 0))
        self.progress_bar = ttkb.Progressbar(pbox2, orient=HORIZONTAL, mode="determinate", maximum=100, value=0)
        self.progress_bar.pack(fill=X)
        irow = ttkb.Frame(pbox2)
        irow.pack(fill=X, pady=(7, 0))
        self.percent_lbl = ttkb.Label(irow, text="0%", font=("Segoe UI", 9, "bold"), width=6)
        self.percent_lbl.pack(side=LEFT)
        ttkb.Label(irow, textvariable=self.speed_var, bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(10, 0))
        ttkb.Label(irow, textvariable=self.eta_var, bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(10, 0))
        ttkb.Label(irow, textvariable=self.size_var, bootstyle="secondary", font=("Segoe UI", 8)).pack(side=LEFT, padx=(10, 0))
        self.fname_lbl = ttkb.Label(irow, text="—", bootstyle="secondary", font=("Segoe UI", 8), wraplength=380, justify=RIGHT)
        self.fname_lbl.pack(side=RIGHT, fill=X, expand=True)
        self.status_lbl = ttkb.Label(pbox2, textvariable=self.status_var, bootstyle="info")
        self.status_lbl.pack(anchor="w", pady=(6, 0))

        # Нижняя панель цепляется к низу окна и никогда не уезжает за экран
        arow2 = ttkb.Frame(self, padding=(20, 8, 20, 12))
        arow2.grid(row=6, column=0, sticky="ew")
        self.btn_download = ttkb.Button(arow2, text="Скачать", width=16, bootstyle="success", command=self._download_async)
        self.btn_download.pack(side=LEFT)
        self.btn_pause = ttkb.Button(arow2, text="Пауза", width=10, bootstyle="warning", command=self._pause_download, state="disabled")
        self.btn_pause.pack(side=LEFT, padx=(8, 0))
        self.btn_stop = ttkb.Button(arow2, text="Остановить", width=16, bootstyle="danger", command=self._stop_download, state="disabled")
        self.btn_stop.pack(side=LEFT, padx=(8, 0))
        self.btn_export_report = ttkb.Button(arow2, text="Сохранить отчёт", width=17, bootstyle="info-outline", command=self._export_report)
        self.btn_export_report.pack(side=LEFT, padx=(8, 0))
        add_tooltip(self.btn_export_report, "Сохранить ZIP с журналами ошибок и версиями компонентов для поддержки")
        ttkb.Button(arow2, text="Папка загрузок", bootstyle="light-outline", command=self._open_dir).pack(side=LEFT, padx=(8, 0))
        self.ytdlp_lbl = ttkb.Label(arow2, text="yt-dlp: ...", bootstyle="secondary", font=("Segoe UI", 8))
        self.ytdlp_lbl.pack(side=RIGHT, padx=(10, 0))

        lbox = ttkb.LabelFrame(self, text=" Журнал ", padding=(12, 8, 12, 8))
        lbox.grid(row=5, column=0, sticky="ew", padx=20, pady=(10, 0))
        self.log_text = tk.Text(lbox, height=4, wrap="word", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4", insertbackground="white", borderwidth=0, padx=10, pady=8, relief="flat")
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        sb = ttkb.Scrollbar(lbox, orient=VERTICAL, command=self.log_text.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.log_text.configure(yscrollcommand=sb.set, state="disabled")

        self._update_state()

    def _update_state(self):
        state = "disabled" if self.playlist_all_var.get() else "normal"
        self.sp_start.configure(state=state)
        self.sp_end.configure(state=state)
        audio = self.format_var.get() == "bestaudio" and not self._selected_format
        self.cmb_af.configure(state="readonly" if audio else "disabled")
        self.cmb_aq.configure(state="readonly" if audio and self.audio_format_var.get() != "wav" else "disabled")
        self._save_config()

    # ---------- форматы ----------

    def _on_mode_change(self):
        self.preset_var.set(QUALITY_PRESETS[0])
        self._selected_format = None
        self._selected_audio_only = False
        self._selected_label = ""
        try:
            sel = self.fmt_tree.selection()
            if sel:
                self.fmt_tree.selection_remove(*sel)
        except Exception:
            LOGGER.debug("Optional operation failed: _on_mode_change", exc_info=True)
        try:
            self.fmt_lbl.config(text="Формат не выбран — используется выбор выше.", bootstyle="secondary")
        except Exception:
            LOGGER.debug("Optional operation failed: _on_mode_change", exc_info=True)
        self._update_state()

    def _current_mode(self):
        """Возвращает (mode, custom_format, custom_audio_only) для загрузки."""
        if self._selected_format:
            return "custom", self._selected_format, self._selected_audio_only
        mode = {"best": "best", "bestvideo": "video", "bestaudio": "audio"}.get(self.format_var.get(), "best")
        return mode, None, False

    @staticmethod
    def _pick_format_source(info: dict) -> dict | None:
        if info.get("entries"):
            for e in info["entries"]:
                if e and e.get("formats"):
                    return e
            return None
        return info

    def _estimate_size(self, info: dict | None = None, height: int | None = None) -> int:
        """Оценка размера до скачивания по данным форматов yt-dlp."""
        src = self._pick_format_source(info if info is not None else self._current_info or {})
        fmts = (src or {}).get("formats") or []
        if not fmts:
            return 0

        def size_of(fmt):
            return fmt.get("filesize") or fmt.get("filesize_approx") or 0

        video = [f for f in fmts if f.get("vcodec") not in (None, "none") and size_of(f)]
        audio = [f for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
        if height:
            limited = [f for f in video if (f.get("height") or 0) <= height]
            video = limited or video
        best_video = max(video, key=size_of) if video else None
        best_audio = max(audio, key=size_of) if audio else None

        if self.format_var.get() == "bestaudio":
            return size_of(best_audio) if best_audio else 0
        if self.format_var.get() == "bestvideo" and not self._selected_format:
            return size_of(best_video) if best_video else 0
        total = (size_of(best_video) if best_video else 0)
        if best_audio and not self._selected_audio_only:
            total += size_of(best_audio)
        return total

    def _on_preset_change(self, _event=None):
        value = self.preset_var.get()
        if value not in QUALITY_PRESETS:
            value = QUALITY_PRESETS[0]
            self.preset_var.set(value)
        try:
            sel = self.fmt_tree.selection()
            if sel:
                self.fmt_tree.selection_remove(*sel)
        except Exception:
            LOGGER.debug("Optional operation failed: _on_preset_change", exc_info=True)

        if value == "Авто":
            self._selected_format = None
            self._selected_label = ""
            self._selected_audio_only = False
            text = "Формат не выбран — используется выбор выше."
            size = 0
        else:
            height = int(value[:-1])
            if self.format_var.get() == "bestaudio":
                self.format_var.set("best")
            self._selected_format = f"bv*[height<={height}]+ba/b[height<={height}]"
            self._selected_label = value
            self._selected_audio_only = False
            size = self._estimate_size(height=height)
            text = f"Пресет: {value}. Видео не выше {height}p со звуком."
        if size:
            text += f" Ожидаемый размер: ~{format_bytes(size)}."
        self.fmt_lbl.config(text=text, bootstyle="secondary")
        self._update_state()

    def _add_fmt_row(self, quality, typ, codec, fps, size, note, fmt, kind):
        if not fmt:
            return
        iid = self.fmt_tree.insert("", "end", values=(quality, typ, codec, fps, size, note))
        self._fmt_map[iid] = (str(fmt), kind)

    def _populate_formats(self, info: dict):
        self._current_info = info or {}
        self.preset_var.set(QUALITY_PRESETS[0])
        for iid in self.fmt_tree.get_children():
            self.fmt_tree.delete(iid)
        self._fmt_map = {}
        self._selected_format = None
        self._selected_audio_only = False
        self._selected_label = ""

        if not info:
            self._update_state()
            self.fmt_lbl.config(text="Формат не выбран — используется выбор выше.", bootstyle="secondary")
            return

        src = self._pick_format_source(info or {})
        fmts = (src or {}).get("formats") or []
        if not fmts:
            self.fmt_lbl.config(text="Форматы недоступны для этой ссылки.", bootstyle="warning")
            return

        def size_of(f):
            return f.get("filesize") or f.get("filesize_approx") or 0

        video = [f for f in fmts if f.get("vcodec") not in (None, "none")]
        audio_only = [f for f in fmts if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
        heights = sorted({f.get("height") for f in video if f.get("height")}, reverse=True)
        best_audio = max(audio_only, key=size_of) if audio_only else None

        self._add_fmt_row("Лучшее", "видео+аудио", "авто", "", "", "самое высокое доступное", "bv*+ba/b", "merge")

        for h in heights:
            cands = [f for f in video if f.get("height") == h]
            v = max(cands, key=size_of)
            fps = v.get("fps")
            codec = f'{v.get("ext", "?")} · {str(v.get("vcodec", "?")).split(".")[0]}'
            total = size_of(v) + (size_of(best_audio) if best_audio else 0)
            self._add_fmt_row(
                f"{h}p", "видео+аудио", codec,
                f"{fps:.0f}" if fps else "",
                format_bytes(total) if total else "",
                "склейка в MP4",
                f"bv*[height<={h}]+ba/b[height<={h}]", "merge",
            )

        for f in sorted(video, key=lambda x: (x.get("height") or 0, size_of(x)), reverse=True):
            h = f.get("height")
            codec = f'{f.get("ext", "?")} · {str(f.get("vcodec", "?")).split(".")[0]}'
            fps = f.get("fps")
            self._add_fmt_row(
                f"{h}p" if h else "видео", "видео (без звука)", codec,
                f"{fps:.0f}" if fps else "",
                format_bytes(size_of(f)) if size_of(f) else "",
                f.get("format_note") or "",
                f.get("format_id"), "video",
            )

        for f in sorted(audio_only, key=size_of, reverse=True)[:12]:
            codec = f'{f.get("ext", "?")} · {str(f.get("acodec", "?")).split(".")[0]}'
            abr = f.get("abr")
            note = f"{abr:.0f} kbps" if abr else (f.get("format_note") or "")
            self._add_fmt_row(
                "аудио", "аудио", codec, "",
                format_bytes(size_of(f)) if size_of(f) else "",
                note, f.get("format_id"), "audio",
            )

        self.fmt_lbl.config(
            text=f"Видео-форматов: {len(video)}, аудио: {len(audio_only)}. Выберите строку — она будет использована при загрузке.",
            bootstyle="secondary",
        )

    def _on_format_select(self, _event=None):
        sel = self.fmt_tree.selection()
        if not sel:
            return
        data = self._fmt_map.get(sel[0])
        if not data:
            return
        self._selected_format, kind = data
        self._selected_audio_only = (kind == "audio")
        self._selected_label = self.fmt_tree.set(sel[0], "quality")
        self.preset_var.set(QUALITY_PRESETS[0])
        size_text = self.fmt_tree.set(sel[0], "size")
        hint = f"Выбран формат: {self._selected_label} ({kind}). Будет использован при загрузке."
        if size_text:
            hint += f" Ожидаемый размер: {size_text}."
        self.fmt_lbl.config(
            text=hint,
            bootstyle="success",
        )
        self._log(f"Выбран формат: {self._selected_label} [{self._selected_format}]")
        self._update_state()

    # ---------- очередь ----------

    def _queue_paste(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        if not text:
            return
        cur = self.queue_input.get("1.0", "end").strip()
        self.queue_input.insert("end", ("\n" if cur else "") + text)

    def _queue_add(self):
        if self._q_running:
            messagebox.showinfo("Очередь", "Дождитесь завершения очереди или остановите её.")
            return
        lines = [l.strip() for l in self.queue_input.get("1.0", "end").splitlines() if l.strip()]
        if not lines:
            messagebox.showinfo("Очередь", "Вставьте хотя бы одну ссылку.")
            return
        added = sum(1 for url in lines if self._queue_append(url))
        self.queue_input.delete("1.0", "end")
        if added:
            self._log(f"В очередь добавлено ссылок: {added}")
            self.status_var.set(f"В очереди: {len(self.queue_items)}")
            self._save_config()

    def _queue_add_from_file(self):
        chosen = filedialog.askopenfilename(title="Файл со ссылками", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not chosen:
            return
        self._queue_add_paths([chosen])

    def _queue_add_paths(self, paths):
        """Добавляет ссылки из текстовых файлов (в том числе перетащенных на .exe)."""
        added = 0
        for chosen in paths:
            try:
                lines = Path(chosen).read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception as e:
                LOGGER.exception("Operation failed: _queue_add_paths")
                messagebox.showerror("Очередь", f"Не удалось прочитать файл:\n{chosen}\n\n{e}")
                continue
            for line in lines:
                url = line.strip()
                if url and not url.startswith("#") and self._queue_append(url):
                    added += 1
        if added:
            self.nb.select(1)
            self.status_var.set(f"В очереди: {len(self.queue_items)}")
            self._log(f"Из файла добавлено ссылок: {added}")
            self._save_config()

    def _queue_insert(self, item: dict) -> bool:
        item.setdefault("num", len(self.queue_items) + 1)
        item.setdefault("status", "ожидание")
        item.setdefault("percent", 0.0)
        item.setdefault("error", "")
        item.setdefault("title", item.get("url", ""))
        item.setdefault("fmt_label", "Лучшее")
        item["iid"] = self.queue_tree.insert(
            "", "end",
            values=(item["num"], (item["title"] or item["url"])[:90], item["fmt_label"],
                    item["status"], f"{item['percent']:.0f}%"),
        )
        self.queue_items.append(item)
        self._queue_update_row(item)
        return True

    def _queue_append(self, url: str) -> bool:
        if self._q_running:
            self._log("Сначала дождитесь завершения очереди или остановите её.")
            return False
        if not looks_like_url(url):
            self._log(f"Пропущена некорректная ссылка: {url[:90]}")
            return False
        if any(it["url"] == url for it in self.queue_items):
            self._log(f"Ссылка уже в очереди: {url[:90]}")
            return False
        try:
            start, end = self._playlist_range()
        except ValueError as e:
            messagebox.showwarning("Диапазон плейлиста", str(e))
            return False
        mode, custom_format, audio_only = self._current_mode()
        if custom_format:
            label = self._selected_label or "формат"
        else:
            label = {"best": "Лучшее", "video": "Видео", "audio": "Аудио"}.get(mode, mode)
        num = len(self.queue_items) + 1
        return self._queue_insert({
            "num": num,
            "url": url,
            "title": url,
            "status": "ожидание",
            "percent": 0.0,
            "error": "",
            "mode": mode,
            "custom_format": custom_format,
            "audio_only": audio_only,
            "audio_format": self.audio_format_var.get(),
            "audio_quality": self.audio_quality_var.get(),
            "start": start,
            "end": end,
            "fmt_label": label,
        })

    def _queue_update_row(self, item: dict):
        try:
            st = item["status"]
            tags = ()
            if st == "готово":
                tags = ("done",)
            elif st == "ошибка":
                tags = ("error",)
            elif st == "пауза":
                tags = ("paused",)
            elif st in ("загрузка", "инфо", "старт", "склейка"):
                tags = ("working",)
            self.queue_tree.item(
                item["iid"],
                values=(
                    item.get("num", ""),
                    (item.get("title") or item["url"])[:90],
                    item.get("fmt_label", "—"),
                    st,
                    f"{item.get('percent', 0.0):.0f}%",
                ),
                tags=tags,
            )
        except Exception:
            LOGGER.debug("Optional operation failed: _queue_update_row", exc_info=True)

    def _queue_post(self, item: dict):
        self._post_ui(lambda it=item: self._queue_update_row(it))

    def _queue_renumber(self):
        for i, it in enumerate(self.queue_items, 1):
            it["num"] = i
            self._queue_update_row(it)

    def _queue_clear(self):
        if self._q_running:
            messagebox.showinfo("Очередь", "Сначала остановите загрузку.")
            return
        for iid in self.queue_tree.get_children():
            self.queue_tree.delete(iid)
        self.queue_items.clear()
        self._log("Очередь очищена")
        self._save_config()

    def _queue_remove_selected(self):
        if self._q_running:
            messagebox.showinfo("Очередь", "Сначала остановите загрузку.")
            return
        sel = set(self.queue_tree.selection())
        if not sel:
            return
        for it in list(self.queue_items):
            if it["iid"] in sel:
                self.queue_items.remove(it)
        for iid in sel:
            try:
                self.queue_tree.delete(iid)
            except Exception:
                LOGGER.debug("Optional operation failed: _queue_remove_selected", exc_info=True)
        self._queue_renumber()
        self._save_config()

    def _queue_pick(self) -> dict | None:
        with self._q_lock:
            for it in self.queue_items:
                if it["status"] == "ожидание":
                    it["status"] = "старт"
                    return it
        return None

    def _queue_start(self):
        if self._q_running or self._updating:
            return
        if self.is_downloading or self._info_loading:
            messagebox.showinfo("Очередь", "Сначала дождитесь завершения текущей загрузки.")
            return
        try:
            n = self.q_parallel_var.get()
            if not 1 <= n <= 4:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showwarning("Очередь", "Число одновременных загрузок должно быть от 1 до 4.")
            return
        pending = [it for it in self.queue_items if it["status"] in ("ожидание", "ошибка", "отмена", "пауза")]
        if not pending:
            messagebox.showinfo("Очередь", "Добавьте ссылки, которые нужно скачать.")
            return
        if not self._check_browsers_before_download():
            return
        self._q_stop.clear()
        self._q_paused = False
        self._save_config()
        self._q_running = True
        self._q_done_count = 0
        self._q_error_count = 0
        for it in pending:
            it["options"] = dict(
                output_dir=Path(self.output_dir_var.get() or str(get_default_download_dir())),
                cookies_file=self.cookies_path,
                cookies_from_browser=self._active_browser(),
                proxy=self.proxy_var.get().strip() or None,
                restrict_filenames=self.restrict_names_var.get(),
                download_archive=self.archive_var.get().strip() if self.skip_downloaded_var.get() else None,
            )
            it["status"] = "ожидание"
            it["percent"] = 0.0
            it["error"] = ""
            self._queue_update_row(it)
        self.btn_q_start.configure(state="disabled", text="Скачать всё")
        self.btn_q_pause.configure(state="normal")
        self.btn_q_stop.configure(state="normal")
        self.status_var.set("Очередь: загрузка...")
        self._log(f"Запускаю очередь из {len(pending)} ссылок (потоков: {max(1, int(self.q_parallel_var.get()))})")
        self._q_threads = []
        for _ in range(n):
            worker = threading.Thread(target=self._queue_worker, daemon=True)
            self._q_threads.append(worker)
            worker.start()
        threading.Thread(target=self._queue_watch, daemon=True).start()

    def _queue_worker(self):
        while not self._q_stop.is_set():
            item = self._queue_pick()
            if item is None:
                return
            self._queue_download_item(item)

    def _queue_download_item(self, item: dict):
        item["status"] = "инфо"
        self._queue_post(item)
        out_dir = item["options"]["output_dir"]
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            LOGGER.exception("Operation failed: _queue_download_item")
            item["status"] = "ошибка"
            item["error"] = str(e)
            self._queue_post(item)
            return

        dl = MediaDownloader(progress_callback=lambda p: self._post_ui(lambda pp=p: self._queue_on_progress(item, pp)))
        with self._q_lock:
            self._q_active[id(item)] = dl
        try:
            if self._q_stop.is_set():
                dl.stop()
            dl.download(
                url=item["url"],
                mode=item["mode"],
                custom_format=item["custom_format"],
                custom_audio_only=item["audio_only"],
                audio_format=item["audio_format"],
                audio_quality=item["audio_quality"],
                playlist_start=item["start"],
                playlist_end=item["end"],
                **item["options"],
            )
            prog = dl.progress
            if prog.status == "done":
                item["status"] = "готово"
                item["percent"] = 100.0
                self._q_done_count += 1
            elif prog.status == "cancelled":
                item["status"] = "пауза" if self._q_paused else "отмена"
            else:
                item["status"] = "ошибка"
                item["error"] = prog.error or "ошибка"
                self._q_error_count += 1
        except Exception as e:
            LOGGER.exception("Operation failed: _queue_download_item")
            item["status"] = "ошибка"
            item["error"] = str(e)
            self._q_error_count += 1
        finally:
            with self._q_lock:
                self._q_active.pop(id(item), None)
            self._queue_post(item)

    def _queue_on_progress(self, item: dict, p: DownloadProgress):
        if item["status"] in ("готово", "ошибка", "отмена"):
            return
        item["percent"] = p.percent
        if p.filename:
            item["title"] = Path(p.filename).name
        if p.status == "downloading":
            item["status"] = "загрузка"
        elif p.status == "merging":
            item["status"] = "склейка"
        elif p.status == "extracting":
            item["status"] = "инфо"
        self._queue_update_row(item)

    def _queue_watch(self):
        for worker in self._q_threads:
            worker.join()
        self._post_ui(self._queue_finish)

    def _queue_finish(self):
        paused = self._q_paused
        self._q_running = False
        try:
            self.btn_q_start.configure(state="normal", text="Продолжить" if paused else "Скачать всё")
            self.btn_q_pause.configure(state="disabled")
            self.btn_q_stop.configure(state="disabled")
        except Exception:
            LOGGER.debug("Optional operation failed: _queue_finish", exc_info=True)
        done = sum(1 for it in self.queue_items if it["status"] == "готово")
        err = sum(1 for it in self.queue_items if it["status"] == "ошибка")
        cancelled = sum(1 for it in self.queue_items if it["status"] == "отмена")
        waiting = sum(1 for it in self.queue_items if it["status"] == "пауза")
        for item in self.queue_items:
            if item["status"] == "ошибка":
                self._log(f"Ошибка: {item['url']} — {item.get('error', 'Неизвестная ошибка')}")
        self._save_config()
        if paused:
            self._log(f"Очередь приостановлена: готово {done}, осталось {waiting}")
            self.status_var.set(f"Очередь приостановлена: готово {done}, осталось {waiting}")
            return
        self._log(f"Очередь завершена: готово {done}, ошибок {err}, отменено {cancelled}")
        self.status_var.set(f"Очередь: готово {done}, ошибок {err}")
        if done and not self._closing:
            folder = Path(self.output_dir_var.get() or str(get_default_download_dir()))
            message = f"Скачано файлов: {done}."
            if err:
                message += f" Не удалось: {err}."
            self._show_completion("Очередь завершена", message, folder)

    def _queue_pause(self):
        if not self._q_running or self._q_paused:
            return
        self._log("Приостанавливаю очередь — загрузки продолжатся с места остановки.")
        self._q_paused = True
        self._q_stop.set()
        with self._q_lock:
            actives = list(self._q_active.values())
            for item in self.queue_items:
                if item["status"] in ("ожидание", "старт"):
                    item["status"] = "пауза"
                    self._post_ui(lambda i=item: self._queue_update_row(i))
        for downloader in actives:
            try:
                downloader.stop()
            except Exception:
                LOGGER.debug("Optional operation failed: _queue_pause", exc_info=True)
        try:
            self.btn_q_pause.configure(state="disabled")
            self.btn_q_stop.configure(state="disabled")
        except Exception:
            LOGGER.debug("Optional operation failed: _queue_pause", exc_info=True)
        self.status_var.set("Приостанавливаю очередь...")

    def _queue_stop(self):
        if not self._q_running:
            return
        self._q_paused = False
        self._log("Останавливаю очередь...")
        self._q_stop.set()
        with self._q_lock:
            actives = list(self._q_active.values())
            for it in self.queue_items:
                if it["status"] in ("ожидание", "старт"):
                    it["status"] = "отмена"
                    self._post_ui(lambda i=it: self._queue_update_row(i))
        for dl in actives:
            try:
                dl.stop()
            except Exception:
                LOGGER.debug("Optional operation failed: _queue_stop", exc_info=True)
        self.btn_q_pause.configure(state="disabled")
        self.btn_q_stop.configure(state="disabled")

    # ---------- log ----------

    def _log(self, msg: str):
        LOGGER.info("%s", msg)
        self._append_log(redact(msg))

    def _append_log(self, msg: str):
        if threading.get_ident() != self._ui_thread:
            self._post_ui(lambda: self._append_log(msg))
            return
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 1500:
            self.log_text.delete("1.0", "250.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _export_report(self):
        if self._exporting_report:
            return
        diagnostic = get_diagnostics() or initialize_logging()
        chosen = filedialog.asksaveasfilename(
            title="Сохранить диагностический отчёт", defaultextension=".zip",
            initialfile=f"Media-Downloader-report-{diagnostic.session_id}.zip",
            filetypes=[("ZIP report", "*.zip")],
        )
        if not chosen:
            return
        self._exporting_report = True
        self.btn_export_report.configure(state="disabled")
        threading.Thread(target=self._export_report_worker, args=(diagnostic, chosen),
                         name="Report-export", daemon=True).start()

    def _export_report_worker(self, diagnostic, chosen):
        try:
            destination = diagnostic.export(chosen)
            self._post_ui(lambda: messagebox.showinfo("Отчёт сохранён", f"{destination}\n\n"
                          "Пришлите этот ZIP вместе с описанием действий перед ошибкой."))
        except Exception as error:
            LOGGER.exception("Could not export diagnostic report")
            self._post_ui(lambda err=redact(error): messagebox.showerror("Не удалось сохранить отчёт", err))
        finally:
            self._post_ui(self._finish_report_export)

    def _finish_report_export(self):
        self._exporting_report = False
        self.btn_export_report.configure(state="normal")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    # ---------- actions ----------

    def _paste_url(self):
        try:
            text = self.clipboard_get().strip()
        except Exception:
            self._log("Буфер обмена пуст")
            return
        if text:
            self.url_var.set(text)
            self._log(f"Ссылка вставлена: {text[:70]}")

    def _clear_url(self):
        if self.is_downloading or self._info_loading:
            return
        self.url_var.set("")
        self.title_lbl.config(text="Название: —")
        self.meta_lbl.config(text="Длительность: — | Канал: —")
        self.status_var.set("Готов к работе")
        self.progress_bar["value"] = 0
        self.percent_lbl.config(text="0%")
        self.speed_var.set("—")
        self.eta_var.set("—")
        self.size_var.set("—")
        self.fname_lbl.config(text="—")
        self._populate_formats({})
        self._log("Ссылка очищена")

    def _browse_dir(self):
        chosen = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(get_default_download_dir()))
        if chosen:
            self.output_dir_var.set(chosen)
            self._save_config()
            self._log(f"Папка сохранения: {chosen}")

    def _open_path(self, path):
        p = Path(path)
        try:
            p.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(p))
            elif sys.platform == "darwin":
                os.system(f'open "{p}"')
            else:
                os.system(f'xdg-open "{p}"')
            self._log(f"Открыта папка: {p}")
        except Exception as e:
            LOGGER.exception("Operation failed: _open_path")
            self._log(f"Не удалось открыть папку: {e}")

    def _open_dir(self):
        self._open_path(self.output_dir_var.get() or str(get_default_download_dir()))

    def _browse_archive(self):
        current = Path(self.archive_var.get() or str(DEFAULT_ARCHIVE_FILE))
        chosen = filedialog.asksaveasfilename(
            title="Файл архива загрузок",
            initialfile=current.name or "download_archive.txt",
            initialdir=str(current.parent),
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if chosen:
            self.archive_var.set(chosen)
            self._save_config()
            self._log(f"Файл архива: {chosen}")

    # ---------- hotkeys / clipboard ----------

    def _setup_shortcuts(self):
        self.bind("<Control-d>", lambda e: self._download_async())
        self.bind("<F5>", lambda e: self._get_info_async())
        self.bind("<Escape>", lambda e: self._stop_download())
        self.bind("<Control-o>", lambda e: self._open_dir())

    def _on_focus_in(self, _event=None):
        """Подставляет ссылку из буфера обмена, если поле пустое."""
        if self.is_downloading:
            return
        try:
            text = self.clipboard_get().strip()
        except Exception:
            return
        if not text or text == self._last_clip:
            return
        self._last_clip = text
        if self.url_var.get().strip() or not looks_like_url(text):
            return
        self.url_var.set(text)
        self._log(f"Ссылка из буфера обмена: {text[:70]}")

    # ---------- yt-dlp update ----------

    def _check_updates_async(self):
        threading.Thread(target=self._check_updates, daemon=True).start()
        threading.Thread(target=self._check_app_update_async, daemon=True).start()

    def _check_app_update_async(self):
        try:
            cur = get_app_version()
            rel = get_latest_app_release()
            if not rel:
                return
            tag = rel.get("tag_name") or ""
            if not is_newer_app_version(tag, cur):
                return
            asset = get_app_asset(rel)
            if not asset or not asset.get("browser_download_url"):
                return
            url = asset["browser_download_url"]
            digest = asset.get("digest") or ""
            self._post_ui(lambda t=tag, u=url, d=digest: self._prompt_app_update(t, u, d))
        except Exception:
            LOGGER.debug("Optional operation failed: _check_app_update_async", exc_info=True)

    def _prompt_app_update(self, tag: str, url: str, digest: str = ""):
        if self._closing or self._updating:
            return
        if self.is_downloading or self._q_running or self._info_loading:
            self.after(60000, lambda t=tag, u=url, d=digest: self._prompt_app_update(t, u, d))
            return
        if not messagebox.askyesno(
            "Обновление программы",
            "Доступна новая версия Media Downloader " + tag + ".\n\nСкачать и обновить сейчас?\nПрограмма закроется и обновится автоматически.",
        ):
            return
        self._do_app_update(url, digest)

    def _do_app_update(self, url: str, digest: str = ""):
        if self._updating:
            return
        if not getattr(sys, "frozen", False):
            messagebox.showinfo("Обновление", "Обновление exe доступно только в собранном .exe")
            return
        self._updating = True
        self.btn_download.configure(state="disabled")
        self.btn_q_start.configure(state="disabled")
        self.btn_info.configure(state="disabled")
        self.status_var.set("Скачиваю обновление программы…")
        threading.Thread(target=self._app_update_worker, args=(url, digest), daemon=True).start()

    def _app_update_worker(self, url: str, digest: str = ""):
        try:
            exe = Path(sys.executable)
            new_exe = exe.with_name("Media-Downloader.new.exe")
            self._log("Скачиваю обновление...")
            if not download_file(url, new_exe, expected_sha256=digest):
                raise RuntimeError("Не удалось скачать обновление или не совпала контрольная сумма")
            bat = create_updater_script(new_exe, exe)
            if not bat:
                raise RuntimeError("Не удалось создать скрипт обновления")
            self._log("Запускаю установщик обновления...")
            self._post_ui(lambda: self._apply_app_update(bat))
        except Exception as e:
            LOGGER.exception("Operation failed: _app_update_worker")
            self._post_ui(lambda err=str(e): self._app_update_failed(err))

    def _apply_app_update(self, bat):
        try:
            subprocess.Popen(["cmd", "/c", str(bat)], shell=False, creationflags=_CREATE_NO_WINDOW)
            self._on_close()
        except Exception as e:
            LOGGER.exception("Operation failed: _apply_app_update")
            self._app_update_failed(str(e))

    def _app_update_failed(self, error):
        self._updating = False
        self.btn_download.configure(state="normal")
        self.btn_q_start.configure(state="normal")
        self.btn_info.configure(state="normal")
        self.status_var.set("Не удалось обновить программу")
        messagebox.showerror("Обновление", error)

    def _check_updates(self):
        current = bootstrap.installed_ytdlp_version()
        self._post_ui(lambda: self._set_ytdlp_bottom(current))
        latest = get_latest_pypi_version("yt-dlp")
        if not latest:
            self._post_ui(lambda: self.ytdlp_lbl2.config(
                    text=f"yt-dlp: установлена {current or '?'}. Не удалось проверить обновления (нет сети).",
                    bootstyle="warning",
                ),
            )
            return
        if is_newer_version(latest, current):
            self._post_ui(lambda: self.ytdlp_lbl2.config(
                    text=f"yt-dlp: установлена {current} — доступна новая версия {latest}.",
                    bootstyle="warning",
                ),
            )
            self._post_ui(lambda: self._log(f"Доступно обновление yt-dlp: {current} → {latest}"))
        else:
            self._post_ui(lambda: self.ytdlp_lbl2.config(
                    text=f"yt-dlp: установлена {current} — это последняя версия.", bootstyle="success"
                ),
            )

    def _set_ytdlp_bottom(self, version: str):
        try:
            self.ytdlp_lbl.config(text=f"yt-dlp: {version or '?'}")
        except Exception:
            LOGGER.debug("Optional operation failed: _set_ytdlp_bottom", exc_info=True)

    def _update_ytdlp_async(self):
        if self._updating:
            return
        if self.is_downloading or self._q_running or self._info_loading:
            messagebox.showinfo("Обновление yt-dlp", "Сначала дождитесь окончания загрузки.")
            return
        self._updating = True
        self.btn_download.configure(state="disabled")
        self.btn_q_start.configure(state="disabled")
        self.btn_info.configure(state="disabled")
        self._log("Обновляю yt-dlp...")
        threading.Thread(target=self._update_ytdlp, daemon=True).start()

    def _update_ytdlp(self):
        try:
            version = bootstrap.install_ytdlp(force=True, log=self._log)
            self._log(f"yt-dlp обновлён до {version or 'последней версии'}.")
            self._post_ui(lambda v=version: messagebox.showinfo(
                "yt-dlp",
                f"yt-dlp обновлён до {v or 'последней версии'}.\n\n"
                "Изменения применятся при следующем запуске программы.",
            ))
        except Exception as e:
            LOGGER.exception("Operation failed: _update_ytdlp")
            self._log(f"Не удалось обновить yt-dlp: {e}")
            self._post_ui(lambda err=str(e): messagebox.showerror("yt-dlp", f"Не удалось обновить yt-dlp:\n\n{err}"))
        finally:
            self._post_ui(self._check_updates_async)
            self._post_ui(self._check_components)
            self._post_ui(self._finish_dependency_update)

    def _finish_dependency_update(self):
        self._updating = False
        self.btn_download.configure(state="normal")
        self.btn_q_start.configure(state="normal")
        self.btn_info.configure(state="normal")

    def _load_cookies(self):
        chosen = filedialog.askopenfilename(title="Выберите cookies.txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if chosen:
            self.cookies_path = chosen
            self._log(f"Cookies загружены: {chosen}")
            self._apply_cookie_status(chosen)
            self._save_config()

    def _apply_cookie_status(self, path):
        """Показывает, есть ли в cookies данные входа (SID/LOGIN_INFO)."""
        res = check_cookies_file(path)
        name = Path(path).name
        if res.get("authenticated"):
            self.cookie_lbl.config(text=f"Загружены: {name} — вход подтверждён", bootstyle="success")
        elif res.get("ok"):
            self.cookie_lbl.config(text=f"Загружены: {name} — БЕЗ входа в аккаунт", bootstyle="warning")
            self._log(res.get("detail", ""))
        else:
            self.cookie_lbl.config(text=f"Загружены: {name} (не удалось проверить)", bootstyle="warning")

    def _clear_cookies(self):
        self.cookies_path = None
        self.cookie_lbl.config(text="Cookies не загружены", bootstyle="secondary")
        self._log("Cookies удалены")
        self._save_config()

    def _reset_browser(self):
        self.browser_var.set("без cookies")
        self._refresh_browser_status()
        self._save_config()

    def _refresh_browser_list(self):
        self._detected = detect_browsers()
        self._browser_by_label = {"без cookies": None}
        labels = ["без cookies"]
        for name, _profile in self._detected:
            if browser_reads_ok(name):
                label = name
            else:
                label = f"{name} — нельзя (защита 127+)"
            self._browser_by_label[label] = name
            labels.append(label)

        self.cmb_browser.configure(values=labels)
        current = self.browser_var.get()
        if current in self._browser_by_label:
            self.cmb_browser.set(current)
        else:
            self.browser_var.set("без cookies")
        self._refresh_browser_status()

        usable = [n for n, _ in self._detected if browser_reads_ok(n)]
        if usable:
            self._log(f"Браузеры, из которых можно читать cookies: {', '.join(usable)}")
        elif self._detected:
            self._log("Для найденных браузеров нужен экспорт cookies.txt. Публичные видео можно скачивать без cookies.")

    def _active_browser(self) -> str | None:
        return self._browser_by_label.get(self.browser_var.get())

    def _refresh_browser_status(self):
        browser = self._active_browser()
        blocked = [n for n, _ in self._detected if not browser_reads_ok(n)]

        if browser and not browser_reads_ok(browser):
            self.browser_status.config(
                text=(
                    f"{browser} использует защиту cookies 127+ — читать их напрямую нельзя, "
                    "закрытие браузера не поможет. Экспортируйте cookies.txt расширением Cookie-Editor."
                ),
                bootstyle="danger",
            )
            return

        if not browser:
            if blocked:
                self.browser_status.config(
                    text=(
                        f"Cookies напрямую недоступны: {', '.join(blocked)} используют защиту 127+. "
                        "Нужен cookies.txt из расширения Cookie-Editor."
                    ),
                    bootstyle="warning",
                )
            else:
                self.browser_status.config(text="Cookies из браузера не используются.", bootstyle="secondary")
            return

        if is_browser_running([browser]):
            self.browser_status.config(
                text=f"{browser} сейчас открыт — закройте его полностью.",
                bootstyle="warning",
            )
        else:
            self.browser_status.config(text=f"{browser} закрыт, можно читать cookies.", bootstyle="success")


    def _check_browsers_before_download(self) -> bool:
        browser = self._active_browser()
        if not browser:
            return True
        if not browser_reads_ok(browser):
            if not messagebox.askokcancel(
                "Браузер недоступен",
                f"{browser} использует защиту 127+ — cookies из него прочитать нельзя.\n\n"
                "Вместо этого экспортируйте cookies.txt расширением Cookie-Editor и загрузите его "
                "в «Настройки» → Cookies.\n\n"
                "Продолжить без cookies?",
            ):
                return False
            return True
        if is_browser_running([browser]):
            if not messagebox.askyesno(
                "Браузер открыт",
                f"{browser} сейчас запущен. Cookies не удастся прочитать, пока он открыт.\n\n"
                "Закрыть браузер и продолжить?",
            ):
                return False
        return True

    def _component_label(self, key: str, text: str, style: str) -> None:
        label = getattr(self, "comp_labels", {}).get(key)
        if label is not None:
            label.config(text=text, bootstyle=style)

    def _check_ffmpeg(self):
        ffmpeg = find_ffmpeg()
        ffprobe = find_ffprobe()
        diagnostic = get_diagnostics()
        if diagnostic:
            diagnostic.update_context("ffmpeg", {"ffmpeg": ffmpeg, "ffprobe": ffprobe})
        LOGGER.info("FFmpeg check: ffmpeg=%s ffprobe=%s", ffmpeg, ffprobe)
        if ffmpeg and ffprobe:
            self.ffmpeg_badge.config(text="FFmpeg: найден", bootstyle="success")
            self._component_label("ffmpeg", ffmpeg, "success")
        elif ffmpeg:
            self.ffmpeg_badge.config(text="FFmpeg: неполный", bootstyle="warning")
            self._component_label(
                "ffmpeg",
                "Набор неполный (нет ffprobe). Нажмите «Восстановить FFmpeg».",
                "warning",
            )
            self._log("Найден FFmpeg, но отсутствует ffprobe. Доступно восстановление в настройках.")
        else:
            self.ffmpeg_badge.config(text="FFmpeg: не найден", bootstyle="danger")
            self._component_label(
                "ffmpeg",
                "Не найден — нужен для объединения видео со звуком. Нажмите «Восстановить FFmpeg».",
                "danger",
            )
            self._log("Для объединения видео со звуком и конвертации аудио восстановите FFmpeg в настройках.")

    def _check_components(self):
        status = bootstrap.component_status()
        diagnostic = get_diagnostics()
        if diagnostic:
            diagnostic.update_context("components", status)
            diagnostic.update_context("youtube_components", {
                "deno": status["deno"]["path"],
                "po_token_installed": status["pot"]["ready"],
                "po_token_running": status["pot"]["running"],
            })
            LOGGER.info("Components: %s", status)

        ytdlp = status["ytdlp"]
        if ytdlp["ready"]:
            self._component_label("ytdlp", f"установлен {ytdlp['version'] or ''}".strip(), "success")
        else:
            self._component_label("ytdlp", "не установлен — скачается автоматически при запуске", "danger")

        if status["ffmpeg"]["ready"]:
            self._component_label("ffmpeg", status["ffmpeg"]["path"], "success")
        else:
            self._component_label("ffmpeg", "не установлен — нажмите «Восстановить FFmpeg»", "danger")

        for key, hint in (
            ("deno", "нужен для JavaScript-проверок YouTube"),
            ("node", "нужен для PO Token"),
            ("git", "нужен для установки PO Token"),
        ):
            item = status[key]
            if item["ready"]:
                self._component_label(key, item["path"], "success")
            else:
                self._component_label(key, f"не установлен — {hint}", "warning")

        if status["pot"]["running"]:
            self._component_label("pot", f"сервер запущен ({SERVER_HOST}:{SERVER_PORT})", "success")
        elif status["pot"]["ready"]:
            version = status["pot"].get("version") or ""
            suffix = f" {version}" if version else ""
            self._component_label("pot", f"установлен{suffix}, сервер запустится автоматически", "warning")
        else:
            self._component_label("pot", "не установлен — нажмите «Установить всё»", "warning")

        ok = ytdlp["ready"] and status["ffmpeg"]["ready"]
        if ok:
            self.ffmpeg_badge.config(text="Компоненты: готовы", bootstyle="success")

    def _open_tools_dir(self):
        path = tools_root()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
            self._log(f"Открыта папка компонентов: {path}")
        except Exception as e:
            LOGGER.exception("Operation failed: _open_tools_dir")
            messagebox.showerror("Компоненты", f"Не удалось открыть папку:\n{path}\n\n{e}")

    def _install_all_async(self):
        if self._installing_components:
            return
        if self.is_downloading or self._q_running:
            messagebox.showinfo("Компоненты", "Дождитесь окончания загрузок перед установкой компонентов.")
            return
        self._installing_components = True
        self.btn_install_all.configure(state="disabled", text="Установка...")
        self.btn_ffmpeg.configure(state="disabled")
        threading.Thread(target=self._install_all_components, daemon=True).start()

    def _install_all_components(self):
        self._log("Проверяю компоненты и доустанавливаю недостающие...")
        try:
            errors = bootstrap.ensure_all(log=self._log)
        except Exception as e:
            LOGGER.exception("Operation failed: _install_all_components")
            errors = [str(e)]
        if errors:
            self._log("Не удалось установить: " + "; ".join(errors))
            self._post_ui(lambda errs=list(errors): messagebox.showerror(
                "Компоненты",
                "Не всё удалось установить:\n\n" + "\n".join(errs) + "\n\nПодробности в журнале.",
            ))
        else:
            self._log("Все компоненты готовы.")
            import pot

            if pot.provider_installed() and not pot.server_running():
                self._log("Запускаю PO Token сервер...")
                if pot.start_server(log=self._log):
                    self._log("PO Token работает.")
        self._post_ui(self._finish_install_all)

    def _finish_install_all(self):
        self._installing_components = False
        self.btn_install_all.configure(state="normal", text="Установить всё")
        self.btn_ffmpeg.configure(state="normal")
        self._check_ffmpeg()
        self._check_components()

    def _check_network_async(self):
        self.net_lbl.config(text="Проверяю доступ к YouTube...", bootstyle="secondary")
        threading.Thread(target=self._check_network, daemon=True).start()

    def _check_network(self):
        try:
            res = check_youtube_network()
        except Exception as e:
            LOGGER.exception("Operation failed: _check_network")
            self._post_ui(lambda err=str(e): self.net_lbl.config(text=f"Ошибка проверки: {err}", bootstyle="danger"))
            return
        state = res.get("state")
        detail = res.get("detail", "")
        LOGGER.info("YouTube network diagnostic: state=%s detail=%s", state, detail)
        style = {"ok": "success", "blocked": "danger"}.get(state, "warning")
        self._post_ui(lambda: self.net_lbl.config(text=detail, bootstyle=style))

    def _autostart_pot(self):
        """Тихо поднимает PO Token сервер, если провайдер уже установлен."""
        if not provider_installed() or server_running():
            return
        self._post_ui(lambda: self._log("Запускаю PO Token сервер..."))
        if start_server(log=lambda m: self._post_ui(lambda msg=m: self._log(msg))):
            self._post_ui(self._check_components)

    def _ensure_pot_server(self) -> bool:
        if server_running():
            return True
        if not provider_installed():
            return False
        ok = start_server(log=lambda m: self._log(m))
        self._check_components()
        return ok

    def _download_ffmpeg_async(self):
        if self._installing_ffmpeg:
            return
        if self.is_downloading or self._q_running:
            messagebox.showinfo("FFmpeg", "Дождитесь окончания загрузок перед восстановлением компонентов.")
            return
        self._installing_ffmpeg = True
        self.btn_ffmpeg.configure(state="disabled")
        threading.Thread(target=self._download_ffmpeg, daemon=True).start()

    def _download_ffmpeg(self):
        try:
            target = bootstrap.install_ffmpeg(log=self._log)
            self._post_ui(lambda: messagebox.showinfo("FFmpeg", f"Компоненты обработки видео установлены.\n\n{target}"))
        except Exception as e:
            LOGGER.exception("Operation failed: _download_ffmpeg")
            self._log(f"FFmpeg: {e}")
            self._post_ui(lambda err=str(e): messagebox.showerror("FFmpeg", err))
        finally:
            self._post_ui(self._finish_ffmpeg_install)

    def _finish_ffmpeg_install(self):
        self._installing_ffmpeg = False
        self.btn_ffmpeg.configure(state="normal")
        self._check_ffmpeg()
        self._check_components()

    # ---------- info ----------

    def _get_info_async(self):
        if self._info_loading or self._updating:
            return
        url = self.url_var.get().strip()
        if not looks_like_url(url):
            messagebox.showwarning("Ссылка", "Вставьте корректную ссылку на видео или плейлист.")
            return
        if not self._check_browsers_before_download():
            return
        self._refresh_browser_status()
        self._info_loading = True
        self.btn_info.configure(state="disabled", text="Получаю информацию…")
        self.title_lbl.config(text="Название: загружаю…")
        self._populate_formats({})
        options = dict(cookies_file=self.cookies_path, cookies_from_browser=self._active_browser(),
                       proxy=self.proxy_var.get().strip() or None)
        self._info_downloader = MediaDownloader()
        threading.Thread(target=self._get_info, args=(url, options), daemon=True).start()

    def _get_info(self, url: str, options: dict):
        browser = options["cookies_from_browser"]
        try:
            if browser:
                self._log(f"Получаю информацию через cookies из {browser}...")
            else:
                self._log(f"Получаю информацию: {url}")
            info = self._info_downloader.extract_info(url, **options)
            if not info:
                raise RuntimeError("YouTube не вернул данные по этой ссылке")

            self._post_ui(lambda: self._present_info(url, info))
        except Exception as e:
            LOGGER.exception("Operation failed: _get_info")
            err = str(e)
            self._log(f"Ошибка: {err}")
            self._post_ui(lambda: self.title_lbl.config(text="Не удалось получить информацию"))
            self._post_ui(lambda err=err: messagebox.showerror("Ошибка получения информации", f"{err}{self._hint_for_error(err)}"))
        finally:
            self._post_ui(self._finish_info)

    def _present_info(self, url, info):
        if self.url_var.get().strip() != url:
            return
        is_playlist = "entries" in info
        count = len(info.get("entries") or []) if is_playlist else 1
        title = info.get("playlist_title") or info.get("title") or "Без названия"
        duration = format_duration(info.get("duration"))
        channel = info.get("channel") or info.get("uploader") or "—"
        self.title_lbl.config(text=f"Название: {title}")
        self._populate_formats(info)
        meta = (
            f"{'Плейлист' if is_playlist else 'Видео'} · {count} шт."
            f" · Длительность: {duration} · Канал: {channel}"
            + (f" · Качество: {info['height']}p" if info.get("height") else "")
        )
        estimate = self._estimate_size(info)
        if estimate and not is_playlist:
            meta += f" · Ожидаемый размер: ~{format_bytes(estimate)}"
        self.meta_lbl.config(text=meta)
        self._log(f"Готово: {title} ({count} шт.)")

    def _finish_info(self):
        self._info_loading = False
        self._info_downloader = None
        self.btn_info.configure(state="normal", text="Получить информацию")

    @staticmethod
    def _hint_for_error(err: str) -> str:
        low = err.lower()
        if any(
            m in low
            for m in (
                "connection reset",
                "connection aborted",
                "unable to download api page",
                "getaddrinfo",
                "failed to resolve",
                "connection refused",
                "remote end closed",
                "ssl",
                "timed out",
            )
        ):
            return (
                "\n\nПохоже, ваша сеть блокирует YouTube (частая ситуация в России — блокировка по имени домена/SNI).\n\n"
                "Ни один локальный загрузчик в такой сети качать не сможет — проблема не в программе.\n\n"
                "Что делать:\n"
                "1. Включите VPN\n"
                "2. Либо укажите прокси в «Настройки» → Сеть и прокси\n"
                "3. Нажмите там же «Проверить сеть», чтобы подтвердить причину\n\n"
                "Формат прокси: http://хост:порт или socks5://хост:порт"
            )
        if "could not copy" in low and "cookie" in low:
            return (
                "\n\nБраузер держит файл cookies заблокированным.\n\n"
                "Что делать:\n"
                "1. Полностью закройте браузер (не только окно — проверьте трей и диспетчер задач)\n"
                "2. Повторите попытку\n\n"
                "Если не помогло — выберите другой браузер в «Настройки» → Cookies, "
                "либо загрузите cookies.txt вручную."
            )
        if "could not find" in low and "cookie" in low or "no such file" in low and "cookie" in low:
            return (
                "\n\nУ выбранного браузера нет файлов cookies — значит, вы в нём не авторизованы на YouTube.\n\n"
                "Войдите в YouTube в этом браузере и повторите."
            )
        if "failed to decrypt with dpapi" in low or ("decrypt" in low and "app-bound" in low):
            return (
                "\n\nChrome/Edge 127+ шифруют cookies ключом, привязанным к самому браузеру. "
                "Внешняя программа их не расшифрует — это ограничение безопасности, закрытие браузера НЕ поможет.\n\n"
                "Что делать:\n"
                "1. Установите расширение Cookie-Editor в Chrome/Edge\n"
                "2. Откройте youtube.com (под своим аккаунтом) и экспортируйте cookies в Netscape (.txt)\n"
                "3. «Настройки» → Cookies → «Загрузить» → выберите файл\n\n"
                "Альтернатива: Firefox читается напрямую без ограничений."
            )
        if "decrypt" in low or "app-bound" in low or "dpapi" in low:
            return (
                "\n\nНе удалось расшифровать cookies.\n\n"
                "Закройте браузер и повторите; выберите другой браузер; "
                "либо загрузите cookies.txt вручную через «Настройки» → Cookies."
            )
        if "age" in low or "confirm your age" in low:
            return "\n\nПохоже на возрастное ограничение — нужны cookies из браузера, где вы вошли в YouTube."
        if "private" in low:
            return "\n\nВидео приватное — доступно только по ссылке для приглашённых."
        if "unavailable" in low or "not available" in low:
            return "\n\nВидео удалено или недоступно в вашем регионе."
        if "sign in" in low or "bot" in low or "429" in low:
            return (
                "\n\nYouTube требует подтверждение «я не робот».\n\n"
                "Что делать:\n"
                "1. Экспортируйте cookies.txt расширением Cookie-Editor, будучи авторизованным на YouTube\n"
                "2. Загрузите файл в «Настройки» → Cookies\n\n"
                "Важно: годятся только cookies СО входом. Если в файле нет SID и LOGIN_INFO — "
                "YouTube всё равно считает вас гостем (в «Настройках» будет пометка «БЕЗ входа в аккаунт»).\n"
                "На Chrome/Edge 127+ cookies напрямую из браузера не читаются — только файлом."
            )
        return ""

    # ---------- download ----------

    def _download_async(self):
        if self._paused_download and not self.is_downloading:
            if self._info_loading or self._updating:
                return
            if self._resume_download():
                return
        if self.is_downloading or self._updating or self._info_loading:
            return
        if self._q_running:
            messagebox.showinfo("Загрузка", "Сначала дождитесь завершения очереди или остановите её.")
            return
        url = self.url_var.get().strip()
        if not looks_like_url(url):
            messagebox.showwarning("Ссылка", "Вставьте корректную ссылку на видео или плейлист.")
            return
        try:
            start, end = self._playlist_range()
        except ValueError as e:
            messagebox.showwarning("Диапазон плейлиста", str(e))
            return

        out_dir = Path(self.output_dir_var.get() or str(get_default_download_dir()))
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            LOGGER.exception("Operation failed: _download_async")
            messagebox.showerror("Ошибка", f"Не могу создать папку:\n{out_dir}\n\n{e}")
            return

        if not self._check_browsers_before_download():
            return

        self.is_downloading = True
        self._paused_download = False
        self.downloader = MediaDownloader(progress_callback=self.on_progress)
        self._save_config()
        self.btn_download.configure(state="disabled", text="Скачать")
        self.btn_pause.configure(state="normal")
        self.btn_stop.configure(state="normal")
        self.progress_bar["value"] = 0
        self.percent_lbl.config(text="0%")
        self.status_var.set("Подготовка...")
        self._log(f"Старт загрузки в {out_dir}")

        mode, custom_format, custom_audio_only = self._current_mode()
        args = dict(
            url=url,
            output_dir=out_dir,
            mode=mode,
            custom_format=custom_format,
            custom_audio_only=custom_audio_only,
            audio_format=self.audio_format_var.get(),
            audio_quality=self.audio_quality_var.get(),
            playlist_start=start,
            playlist_end=end,
            cookies_file=self.cookies_path,
            cookies_from_browser=self._active_browser(),
            proxy=self.proxy_var.get().strip() or None,
            restrict_filenames=self.restrict_names_var.get(),
            download_archive=self.archive_var.get().strip() if self.skip_downloaded_var.get() else None,
        )
        self._last_download_args = dict(args)
        threading.Thread(target=self._worker, args=(args,), daemon=True).start()

    def _resume_download(self) -> bool:
        """Продолжает приостановленную загрузку: yt-dlp докачивает с места остановки."""
        args = self._last_download_args
        if not args or args.get("url") != self.url_var.get().strip():
            self._paused_download = False
            self._last_download_args = None
            self.btn_download.configure(text="Скачать")
            return False
        self.is_downloading = True
        self._paused_download = False
        self.downloader = MediaDownloader(progress_callback=self.on_progress)
        self.btn_download.configure(state="disabled", text="Скачать")
        self.btn_pause.configure(state="normal")
        self.btn_stop.configure(state="normal")
        self.status_var.set("Продолжаю загрузку...")
        self._log("Продолжаю загрузку с места остановки")
        threading.Thread(target=self._worker, args=(dict(args),), daemon=True).start()
        return True

    def _worker(self, args: dict):
        try:
            self.downloader.download(**args)
        except Exception as e:
            LOGGER.exception("Operation failed: _worker")
            self.downloader.progress.status = "error"
            self.downloader.progress.error = str(e)
            self._log(f"Критическая ошибка: {e}")
        finally:
            final = self.downloader.progress
            if final.status == "done":
                self._log("Загрузка завершена успешно")
                folder = args["output_dir"]
                message = "Файл сохранён."
                if final.filename:
                    message = f"Файл: {Path(final.filename).name}"
                self._post_ui(lambda: self._show_completion("Загрузка завершена", message, folder))
            elif final.status == "cancelled":
                if self._paused_download:
                    self._log("Загрузка приостановлена")
                else:
                    self._log("Загрузка отменена")
            elif final.status == "error":
                self._log(f"Ошибка загрузки: {final.error}")
                self._post_ui(lambda: messagebox.showerror(
                        "Ошибка загрузки",
                        f"{final.error or 'Неизвестная ошибка'}{self._hint_for_error(final.error or '')}",
                    ),
                )
            self._post_ui(self._finish)

    def _finish(self):
        self.is_downloading = False
        self._save_config()
        self.btn_stop.configure(state="disabled")
        self.btn_pause.configure(state="disabled")
        if self._paused_download:
            self.btn_download.configure(state="normal", text="Продолжить")
        else:
            self.btn_download.configure(state="normal", text="Скачать")
            if self.downloader.progress.status in ("done", "error"):
                self._last_download_args = None

    def _pause_download(self):
        if not self.is_downloading or self._paused_download:
            return
        self._paused_download = True
        self._log("Пауза. Продолжить можно кнопкой «Продолжить».")
        self.downloader.stop()
        self.btn_pause.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("Пауза")

    def _stop_download(self):
        if not self.is_downloading:
            return
        self._paused_download = False
        self._last_download_args = None
        self._log("Останавливаю...")
        self.downloader.stop()
        self.btn_pause.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("Останавливаю...")

    def _flash_taskbar(self):
        if os.name != "nt":
            return
        try:
            import ctypes

            handle = int(self.wm_frame(), 16)
            ctypes.windll.user32.FlashWindow(handle, True)
        except Exception:
            LOGGER.debug("Could not flash taskbar", exc_info=True)

    def _show_completion(self, title: str, message: str, folder):
        """Немодальное уведомление с кнопкой «Открыть папку»."""
        self._flash_taskbar()
        try:
            self.bell()
        except Exception:
            LOGGER.debug("Could not ring bell", exc_info=True)
        try:
            win = tk.Toplevel(self)
            win.title(title)
            win.transient(self)
            win.resizable(False, False)
            body = ttkb.Frame(win, padding=(18, 16, 18, 14))
            body.pack(fill=BOTH, expand=True)
            ttkb.Label(body, text=title, font=("Segoe UI Semibold", 12)).pack(anchor="w")
            ttkb.Label(body, text=message, wraplength=420, justify=LEFT,
                       bootstyle="secondary").pack(anchor="w", pady=(6, 0))
            ttkb.Label(body, text=str(folder), wraplength=420, justify=LEFT,
                       font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 0))
            row = ttkb.Frame(body)
            row.pack(fill=X, pady=(14, 0))
            ttkb.Button(row, text="Открыть папку", bootstyle="primary",
                        command=lambda: (self._open_path(folder), win.destroy())).pack(side=LEFT)
            ttkb.Button(row, text="Закрыть", bootstyle="light-outline",
                        command=win.destroy).pack(side=LEFT, padx=(8, 0))
            win.update_idletasks()
            x = self.winfo_rootx() + max(0, (self.winfo_width() - win.winfo_width()) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - win.winfo_height()) // 3)
            win.geometry(f"+{max(0, x)}+{max(0, y)}")
            win.attributes("-topmost", True)
            win.after(1500, lambda: self._drop_topmost(win))
        except Exception:
            LOGGER.debug("Could not show completion window", exc_info=True)

    @staticmethod
    def _drop_topmost(win):
        try:
            win.attributes("-topmost", False)
        except Exception:
            LOGGER.debug("Optional operation failed: _drop_topmost", exc_info=True)

    def on_progress(self, p: DownloadProgress):
        self._post_ui(lambda: self._render(p))

    def _render(self, p: DownloadProgress):
        if p.status == "extracting":
            if p.retry_message:
                self.status_var.set(p.retry_message)
            else:
                self.status_var.set("Получаю информацию…")
        elif p.status == "downloading":
            counter = f" [{p.current_index}/{p.total_count}]" if p.is_playlist and p.total_count else ""
            self.status_var.set(f"Загрузка{counter}...")
            pct = round(p.percent, 1)
            self.progress_bar["value"] = pct
            self.percent_lbl.config(text=f"{pct}%")
            self.speed_var.set(f"Скорость: {format_bytes(int(p.speed))}/с" if p.speed > 0 else "Скорость: —")
            self.eta_var.set(f"Осталось: {format_eta(p.eta)}")
            if p.total > 0:
                self.size_var.set(f"{format_bytes(p.downloaded)} / {format_bytes(p.total)}")
            else:
                self.size_var.set(format_bytes(p.downloaded))
            if p.filename:
                self.fname_lbl.config(text=Path(p.filename).name)
        elif p.status == "merging":
            self.progress_bar["value"] = 99
            self.percent_lbl.config(text="99%")
            self.status_var.set("Обрабатываю файл…")
            self.speed_var.set("Скорость: —")
        elif p.status == "done":
            self.progress_bar["value"] = 100
            self.percent_lbl.config(text="100%")
            self.status_var.set("Готово")
            self.speed_var.set("Скорость: —")
            self.eta_var.set("Осталось: —")
            self.size_var.set("Готово")
        elif p.status == "cancelled":
            self.status_var.set("Отменено")
        elif p.status == "error":
            self.status_var.set("Ошибка")

    def _on_close(self):
        busy = self.is_downloading or self._q_running
        if busy:
            if not messagebox.askyesno("Выход", "Идёт загрузка. Остановить и выйти?"):
                return
            self.downloader.stop()
            self._queue_stop()
        self._save_config()
        LOGGER.info("Window close requested")
        self._closing = True
        if self._info_downloader is not None:
            self._info_downloader.stop()
        threading.Thread(target=stop_server).start()
        self.destroy()


def main():
    initialize_logging(notify_fatal=True)
    app = YTApp()
    paths = [arg for arg in sys.argv[1:] if Path(arg).is_file()]
    if paths:
        app.after(300, lambda: app._queue_add_paths(paths))
    LOGGER.info("Application window ready")
    try:
        app.mainloop()
    finally:
        LOGGER.info("Application mainloop exited")


if __name__ == "__main__":
    main()
