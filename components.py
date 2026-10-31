"""Установка внешних компонентов и проверка их готовности.

Здесь живёт всё, что скачивает и распаковывает yt-dlp, FFmpeg, Deno, Node.js,
Git и PO Token provider. Модуль ничего не знает про порядок первого запуска и
окно прогресса — этим занимается ``bootstrap.py``.

Все функции блокирующие: их нужно вызывать из фонового потока, а прогресс
показывать через ``log`` и ``progress``.
"""

import importlib
import importlib.util
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import net

LOGGER = logging.getLogger("media_downloader.components")

# mutagen нужен yt-dlp, чтобы вкладывать обложку и теги в mp3, m4a, flac и opus.
# Без него файл скачивается, но остаётся без обложки, поэтому пакет ставится
# вместе с движком, а при обновлении — доустанавливается отдельно.
PYPI_PACKAGES = ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider", "mutagen")

_PACKAGE_MODULES = {
    "yt-dlp": "yt_dlp",
    "yt-dlp-ejs": "yt_dlp_ejs",
    "bgutil-ytdlp-pot-provider": "yt_dlp_plugins.extractor.getpot_bgutil_http",
    "mutagen": "mutagen",
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
    import sys

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


def embed_ready() -> bool:
    """Есть ли mutagen: без него обложка не встраивается в аудиофайлы."""
    return _importable("mutagen")


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
    ffmpeg = utils.find_ffmpeg()
    ffprobe = utils.find_ffprobe()
    if not (ffmpeg and ffprobe):
        return False
    # Свою сборку проверяем запуском: у shared-версии рядом должны лежать DLL,
    # иначе ffmpeg.exe существует, но падает при склейке. Чужой FFmpeg из PATH
    # не трогаем — его работоспособность проверяет сам yt-dlp.
    try:
        managed = utils.ffmpeg_cache_dir().resolve()
        if managed in Path(ffmpeg).resolve().parents:
            return utils.tool_works(ffmpeg) and utils.tool_works(ffprobe)
    except OSError:
        return False
    return True


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
        "embed": {"ready": embed_ready()},
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

def _get_json(url: str, timeout: float = 20):
    return net.get_json(url, timeout=timeout)


def _download(url: str, destination: Path, log=None, progress=None,
              timeout=(20, 120), expected_sha256: str | None = None) -> Path:
    emit = _emit(log)
    emit(f"Скачиваю {destination.name}...")
    return net.download(
        url,
        destination,
        log=emit,
        progress=_report(progress),
        timeout=timeout,
        expected_sha256=expected_sha256,
    )


def _extract_zip(archive: Path, destination: Path) -> None:
    """Распаковывает zip и убирает единственную верхнюю папку, если она есть."""
    with tempfile.TemporaryDirectory(prefix="media-downloader-unpack-") as folder:
        root = Path(folder)
        net.safe_extract_zip(archive, root)
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


# --------------------------------------------------------------------------
# yt-dlp и плагин PO Token (wheel с PyPI, без pip)
# --------------------------------------------------------------------------

def _pypi_wheel(package: str) -> tuple[str, str, str]:
    data = _get_json(f"https://pypi.org/pypi/{package}/json")
    version = (data.get("info") or {}).get("version") or ""
    files = data.get("urls") or []
    wheels = [item for item in files if item.get("packagetype") == "bdist_wheel"]
    pure = [item for item in wheels if item.get("filename", "").endswith("-none-any.whl")]
    chosen = (pure or wheels or [None])[0]
    if not chosen or not version:
        raise RuntimeError(f"Для пакета {package} не найден wheel-архив на PyPI.")
    digest = ((chosen.get("digests") or {}).get("sha256") or "").strip()
    if not digest:
        raise RuntimeError(f"PyPI не сообщил контрольную сумму для {package}.")
    return version, chosen["url"], digest


def _install_wheel(archive: Path) -> None:
    """Распаковывает wheel в папку pylibs, заменяя старую версию пакета."""
    libs = _utils().pylibs_dir()
    libs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="media-downloader-wheel-") as folder:
        root = Path(folder)
        net.safe_extract_zip(archive, root)
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
    """Ставит (или обновляет) yt-dlp, yt-dlp-ejs, bgutil-плагин и mutagen."""
    emit = _emit(log)
    report = _report(progress)
    current = installed_ytdlp_version()
    if force:
        pending = list(PYPI_PACKAGES)
    else:
        pending = [package for package in PYPI_PACKAGES
                   if not _importable(_PACKAGE_MODULES[package])]
    if not pending:
        emit(f"yt-dlp уже установлен ({current or 'готов'})")
        report(1.0)
        return current

    selected = []
    for package in pending:
        version, url, digest = _pypi_wheel(package)
        selected.append((package, version, url, digest))

    for index, (package, version, url, digest) in enumerate(selected):
        emit(f"Устанавливаю {package} {version}...")

        def stage(fraction, index=index):
            report((index + max(0.0, min(fraction, 1.0))) / len(selected))

        with tempfile.TemporaryDirectory(prefix="media-downloader-wheel-dl-") as folder:
            archive = _download(url, Path(folder) / f"{package}.whl", log=emit,
                                progress=stage, expected_sha256=digest)
            _install_wheel(archive)
        emit(f"{package} {version} готов")

    report(1.0)
    version = installed_ytdlp_version()
    LOGGER.info("yt-dlp installed: %s", version)
    return version


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
