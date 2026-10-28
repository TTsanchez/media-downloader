"""Проверка и доустановка компонентов при запуске.

Сборка «lite» содержит только сам интерфейс. Всё, что нужно для скачивания
видео, при первом запуске автоматически кладётся в
``%LOCALAPPDATA%\\Media-Downloader\\tools``:

* yt-dlp, yt-dlp-ejs и bgutil-плагин — из wheel-архивов PyPI (без pip);
* FFmpeg и ffprobe — статическая сборка с GitHub;
* Deno, Node.js и MinGit — портативные zip-архивы, без установщиков и прав
  администратора;
* PO Token provider — собирается локально из Node.js и Git по требованию.

Все функции блокирующие: их нужно вызывать из фонового потока, а в GUI
показывать прогресс через ``log`` и ``progress``.
"""

import importlib
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

LOGGER = logging.getLogger("media_downloader.bootstrap")

_USER_AGENT = {"User-Agent": "Media-Downloader", "Accept": "application/vnd.github+json"}

PYPI_PACKAGES = ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider")
STAGE_WEIGHTS = {
    "yt-dlp": 25,
    "ffmpeg": 25,
    "deno": 15,
    "node": 15,
    "git": 10,
    "po": 10,
}


def _utils():
    import utils

    return utils


def _emit(log):
    return log or (lambda _message: None)


def _report(progress):
    return progress or (lambda _fraction: None)


# --------------------------------------------------------------------------
# пути и импорт скачанных пакетов
# --------------------------------------------------------------------------

def prepare_imports() -> None:
    """Ставит папку со скачанными python-пакетами в начало sys.path.

    Вызывать до импорта ``yt_dlp`` / ``downloader``: тогда обновлённый
    yt-dlp из ``tools\\pylibs`` важнее встроенного в сборку.
    """
    libs = _utils().pylibs_dir()
    try:
        libs.mkdir(parents=True, exist_ok=True)
    except OSError:
        LOGGER.warning("Could not create package directory: %s", libs, exc_info=True)
    path = str(libs)
    if path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)
    importlib.invalidate_caches()


def _pylibs_file(*parts) -> bool:
    return _utils().pylibs_dir().joinpath(*parts).is_file()


def _importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def ytdlp_ready() -> bool:
    return (
        _importable("yt_dlp")
        and _importable("yt_dlp_ejs")
        and _importable("yt_dlp_plugins.extractor.getpot_bgutil_http")
    )


def installed_ytdlp_version() -> str:
    version_file = _utils().pylibs_dir() / "yt_dlp" / "version.py"
    try:
        match = re.search(
            r"__version__\s*=\s*['\"]([^'\"]+)",
            version_file.read_text(encoding="utf-8", errors="replace"),
        )
        if match:
            return match.group(1)
    except OSError:
        pass
    # В режиме исходников yt-dlp стоит в venv, а не в tools\pylibs.
    return _utils().get_installed_ytdlp_version()


def ffmpeg_ready() -> bool:
    utils = _utils()
    return bool(utils.find_ffmpeg() and utils.find_ffprobe())


def deno_ready() -> bool:
    import pot

    return bool(pot.find_deno())


def node_ready() -> bool:
    import pot

    return bool(pot.find_node())


def git_ready() -> bool:
    import pot

    return bool(pot._find_git())


def pot_ready() -> bool:
    import pot

    return bool(pot.provider_installed())


def component_status() -> dict:
    """Краткое состояние всех компонентов для раздела «Настройки»."""
    import pot

    utils = _utils()
    return {
        "ytdlp": {"ready": ytdlp_ready(), "version": installed_ytdlp_version()},
        "ffmpeg": {"ready": ffmpeg_ready(), "path": utils.find_ffmpeg() or ""},
        "deno": {"ready": deno_ready(), "path": pot.find_deno() or ""},
        "node": {"ready": node_ready(), "path": pot.find_node() or ""},
        "git": {"ready": git_ready(), "path": pot._find_git() or ""},
        "pot": {"ready": pot_ready(), "running": pot.server_running(), "version": pot.provider_version()},
        "root": str(utils.tools_root()),
    }


# --------------------------------------------------------------------------
# загрузка и распаковка
# --------------------------------------------------------------------------

def _session():
    """HTTP-сеанс с повторами: первый запуск не должен падать из-за одного сбоя TLS."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    session = requests.Session()
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
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_USER_AGENT)
    return session


def _get_json(url: str, timeout: float = 20):
    with _session() as session:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()


def _download(url: str, destination: Path, log=None, progress=None, timeout=(20, 120)) -> Path:
    emit = _emit(log)
    report = _report(progress)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    LOGGER.info("Download started: url=%s destination=%s", url, destination)
    emit(f"Скачиваю {destination.name}...")

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with _session() as session, session.get(url, stream=True, timeout=timeout) as response:
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                received = 0
                with partial.open("wb") as output:
                    for chunk in response.iter_content(1 << 20):
                        if not chunk:
                            continue
                        output.write(chunk)
                        received += len(chunk)
                        if total:
                            report(min(received / total, 1.0))
            partial.replace(destination)
            emit(f"Загружено: {destination.name}")
            return destination
        except Exception as error:
            last_error = error
            LOGGER.warning("Download attempt %s failed: %s", attempt, url, exc_info=True)
            partial.unlink(missing_ok=True)
            if attempt < 3:
                emit("Сбой сети, повторяю загрузку...")
                time.sleep(2 * attempt)
    raise RuntimeError(f"Не удалось скачать {destination.name}: {last_error}")


def _extract_zip(archive: Path, destination: Path) -> None:
    """Распаковывает zip и убирает единственную верхнюю папку, если она есть."""
    with tempfile.TemporaryDirectory(prefix="media-downloader-unpack-") as folder:
        root = Path(folder)
        with zipfile.ZipFile(archive) as package:
            package.extractall(root)
        entries = list(root.iterdir())
        source = entries[0] if len(entries) == 1 and entries[0].is_dir() else root
        destination.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            target = destination / item.name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            shutil.move(str(item), str(target))


def _github_release(repo: str) -> dict:
    return _get_json(f"https://api.github.com/repos/{repo}/releases/latest")


def _tool_version(executable: str, args: list[str]) -> str:
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if result.returncode == 0:
            return (result.stdout or result.stderr).strip().splitlines()[0]
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
# yt-dlp и плагин PO Token (wheel с PyPI, без pip)
# --------------------------------------------------------------------------

def _pypi_wheel(package: str) -> tuple[str, str]:
    data = _get_json(f"https://pypi.org/pypi/{package}/json")
    version = (data.get("info") or {}).get("version") or ""
    files = data.get("urls") or []
    wheels = [item for item in files if item.get("packagetype") == "bdist_wheel"]
    pure = [item for item in wheels if item.get("filename", "").endswith("-none-any.whl")]
    chosen = (pure or wheels or [None])[0]
    if not chosen or not version:
        raise RuntimeError(f"Для пакета {package} не найден wheel-архив на PyPI.")
    return version, chosen["url"]


def _install_wheel(archive: Path) -> None:
    """Распаковывает wheel в папку pylibs, заменяя старую версию пакета."""
    libs = _utils().pylibs_dir()
    libs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="media-downloader-wheel-") as folder:
        root = Path(folder)
        with zipfile.ZipFile(archive) as package:
            package.extractall(root)
        for entry in root.iterdir():
            if entry.name == "yt_dlp_plugins":
                # namespace-пакет: не удаляем, чтобы не потерять другие плагины
                for item in entry.rglob("*"):
                    if item.is_dir():
                        continue
                    target = libs / item.relative_to(root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
                continue
            if entry.name.endswith(".dist-info"):
                project = entry.name.split("-")[0].lower().replace("_", "-")
                for old in libs.glob("*.dist-info"):
                    if old.name.lower().replace("_", "-").startswith(project + "-"):
                        shutil.rmtree(old, ignore_errors=True)
                shutil.copytree(entry, libs / entry.name, dirs_exist_ok=True)
                continue
            target = libs / entry.name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            shutil.move(str(entry), str(target))
    importlib.invalidate_caches()


def install_ytdlp(force: bool = False, log=None, progress=None) -> str:
    """Ставит (или обновляет) yt-dlp, yt-dlp-ejs и bgutil-плагин."""
    emit = _emit(log)
    report = _report(progress)
    current = installed_ytdlp_version()
    if not force and ytdlp_ready():
        emit(f"yt-dlp уже установлен ({current or 'готов'})")
        report(1.0)
        return current

    selected = []
    for package in PYPI_PACKAGES:
        version, url = _pypi_wheel(package)
        selected.append((package, version, url))

    for index, (package, version, url) in enumerate(selected):
        emit(f"Устанавливаю {package} {version}...")

        def stage(fraction, index=index):
            report((index + max(0.0, min(fraction, 1.0))) / len(selected))

        with tempfile.TemporaryDirectory(prefix="media-downloader-wheel-dl-") as folder:
            archive = _download(url, Path(folder) / f"{package}.whl", log=emit, progress=stage)
            _install_wheel(archive)
        emit(f"{package} {version} готов")

    report(1.0)
    version = installed_ytdlp_version()
    LOGGER.info("yt-dlp installed: %s", version)
    return version


def latest_ytdlp_version() -> str | None:
    try:
        return _pypi_wheel("yt-dlp")[0]
    except Exception:
        LOGGER.warning("Could not check yt-dlp version on PyPI", exc_info=True)
        return None


# --------------------------------------------------------------------------
# FFmpeg
# --------------------------------------------------------------------------

def install_ffmpeg(log=None, progress=None) -> Path:
    from ffmpeg_tools import install_ffmpeg as _install

    emit = _emit(log)
    emit("Устанавливаю FFmpeg и ffprobe...")
    result = _install(_utils().ffmpeg_cache_dir(), log=emit, progress=_report(progress))
    _report(progress)(1.0)
    return result


# --------------------------------------------------------------------------
# Deno, Node.js, Git (портативные архивы)
# --------------------------------------------------------------------------

def install_deno(force: bool = False, log=None, progress=None) -> Path:
    import pot

    emit = _emit(log)
    target = _utils().deno_dir()
    executable = target / ("deno.exe" if os.name == "nt" else "deno")
    if executable.is_file() and not force:
        emit(f"Deno уже установлен ({executable})")
        _report(progress)(1.0)
        return executable

    emit("Ищу последнюю версию Deno...")
    release = _github_release("denoland/deno")
    assets = release.get("assets") or []
    wanted = "deno-x86_64-pc-windows-msvc.zip" if os.name == "nt" else "deno-x86_64-unknown-linux-gnu.zip"
    asset = next((item for item in assets if item.get("name") == wanted), None)
    if not asset:
        raise RuntimeError("В релизе Deno не найден архив для этой системы.")

    with tempfile.TemporaryDirectory(prefix="media-downloader-deno-") as folder:
        archive = _download(asset["browser_download_url"], Path(folder) / wanted, log=emit, progress=progress)
        _extract_zip(archive, target)
    if not executable.is_file():
        raise RuntimeError("После распаковки Deno не найден deno.exe.")
    emit(f"Deno установлен: {pot.find_deno() or executable}")
    _report(progress)(1.0)
    return executable


def _node_dist() -> tuple[str, str]:
    for entry in _get_json("https://nodejs.org/dist/index.json"):
        if entry.get("lts"):
            version = entry["version"]
            return version, f"https://nodejs.org/dist/{version}/node-{version}-win-x64.zip"
    raise RuntimeError("Не удалось определить актуальную LTS-версию Node.js.")


def install_node(force: bool = False, log=None, progress=None) -> Path:
    emit = _emit(log)
    target = _utils().node_dir()
    executable = target / ("node.exe" if os.name == "nt" else "node")
    if executable.is_file() and not force:
        emit(f"Node.js уже установлен ({executable})")
        _report(progress)(1.0)
        return executable

    emit("Ищу последнюю LTS-версию Node.js...")
    version, url = _node_dist()
    emit(f"Скачиваю Node.js {version}...")
    with tempfile.TemporaryDirectory(prefix="media-downloader-node-") as folder:
        archive = _download(url, Path(folder) / f"node-{version}.zip", log=emit, progress=progress)
        _extract_zip(archive, target)
    if not executable.is_file():
        raise RuntimeError("После распаковки Node.js не найден node.exe.")
    emit(f"Node.js установлен: {executable}")
    _report(progress)(1.0)
    return executable


def install_git(force: bool = False, log=None, progress=None) -> Path:
    emit = _emit(log)
    target = _utils().git_dir()
    executable = target / "cmd" / ("git.exe" if os.name == "nt" else "git")
    if executable.is_file() and not force:
        emit(f"Git уже установлен ({executable})")
        _report(progress)(1.0)
        return executable

    emit("Ищу последнюю версию MinGit...")
    release = _github_release("git-for-windows/git")
    asset = None
    for item in release.get("assets") or []:
        name = (item.get("name") or "").lower()
        if name.startswith("mingit-") and "64-bit" in name and name.endswith(".zip") and "busybox" not in name:
            asset = item
            break
    if not asset:
        raise RuntimeError("В релизе Git для Windows не найден MinGit 64-bit.")

    emit(f"Скачиваю {asset['name']}...")
    with tempfile.TemporaryDirectory(prefix="media-downloader-git-") as folder:
        archive = _download(asset["browser_download_url"], Path(folder) / asset["name"], log=emit, progress=progress)
        _extract_zip(archive, target)
    if not executable.is_file():
        raise RuntimeError("После распаковки Git не найден cmd/git.exe.")
    emit(f"Git установлен: {executable}")
    _report(progress)(1.0)
    return executable


# --------------------------------------------------------------------------
# PO Token provider
# --------------------------------------------------------------------------

def install_po_provider(force: bool = False, log=None, progress=None) -> tuple[bool, str]:
    """Собирает bgutil-провайдер. Требует Node.js и Git."""
    emit = _emit(log)
    import pot

    installed = pot.provider_version()
    if pot.provider_installed() and not force:
        if not installed or installed == pot.PROVIDER_VERSION:
            emit(f"PO Token provider уже установлен ({installed or 'готов'})")
            _report(progress)(1.0)
            return True, str(pot.server_main())
        emit(f"Обновляю PO Token provider {installed} → {pot.PROVIDER_VERSION}...")
        force = True

    if not node_ready():
        install_node(log=emit)
    if not git_ready():
        install_git(log=emit)

    emit("Собираю PO Token provider (2–5 минут)...")
    ok, message = pot.install_provider(log=emit, force=force)
    _report(progress)(1.0)
    return ok, message


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
    """Ставит только то, без чего нельзя скачивать: yt-dlp и FFmpeg."""
    emit = _emit(log)
    errors: list[str] = []
    steps = []
    if not ytdlp_ready():
        steps.append("yt-dlp")
    if not ffmpeg_ready():
        steps.append("ffmpeg")

    for index, name in enumerate(steps):
        def stage(fraction, index=index):
            _report(progress)((index + max(0.0, min(fraction, 1.0))) / len(steps))

        try:
            if name == "yt-dlp":
                install_ytdlp(log=emit, progress=stage)
            else:
                install_ffmpeg(log=emit, progress=stage)
        except Exception as error:
            LOGGER.exception("Could not install component: %s", name)
            errors.append(f"{name}: {error}")
    if steps:
        _report(progress)(1.0)
    return errors


def ensure_all(log=None, progress=None) -> list[str]:
    """Ставит всё, включая тяжёлые компоненты (Deno, Node.js, Git, PO Token)."""
    emit = _emit(log)
    errors: list[str] = []
    stages = [
        ("yt-dlp", lambda stage: install_ytdlp(log=emit, progress=stage)),
        ("ffmpeg", lambda stage: install_ffmpeg(log=emit, progress=stage)),
        ("deno", lambda stage: install_deno(log=emit, progress=stage)),
        ("node", lambda stage: install_node(log=emit, progress=stage)),
        ("git", lambda stage: install_git(log=emit, progress=stage)),
        ("po", lambda stage: _install_po_stage(emit, stage)),
    ]
    total = sum(weight for _name, weight in STAGE_WEIGHTS.items()) or 1
    done_weight = 0
    for name, action in stages:
        weight = STAGE_WEIGHTS.get(name, 10)

        def stage(fraction, done=done_weight, weight=weight):
            _report(progress)((done + weight * max(0.0, min(fraction, 1.0))) / total)

        try:
            action(stage)
        except Exception as error:
            LOGGER.exception("Could not install component: %s", name)
            errors.append(f"{name}: {error}")
        done_weight += weight
        _report(progress)(done_weight / total)
    return errors


def _install_po_stage(log, stage) -> None:
    import pot

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
        width, height = 460, 200
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
        self._done = False
        self._root.update_idletasks()

    def report(self, message: str, fraction: float | None = None) -> None:
        self._queue.put((message, fraction))

    def run(self, worker) -> list[str]:
        threading.Thread(target=self._work, args=(worker,), daemon=True).start()
        self._root.after(80, self._poll)
        self._root.mainloop()
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
            progress=lambda fraction: report(_stage_label(missing, fraction), fraction),
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


def _stage_label(missing: list[str], fraction: float) -> str:
    if not missing:
        return "Готово"
    index = min(int(fraction * len(missing)), len(missing) - 1)
    return f"Устанавливаю: {missing[index]}"


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
