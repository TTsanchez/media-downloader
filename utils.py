import os
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests
from diagnostics import get_diagnostics

LOGGER = logging.getLogger("media_downloader.utils")


GECKO_BROWSERS = {"firefox", "thunderbird", "librewolf", "waterfox"}


def browser_reads_ok(name: str) -> bool:
    """Читает ли yt-dlp cookies этого браузера.

    Chromium 127+ хранит ключ через app-bound encryption — он привязан к самому
    браузеру, и внешняя программа (yt-dlp) расшифровать его не может.
    """
    if name in GECKO_BROWSERS:
        return True
    root = _browser_root(name)
    if root is None:
        return True
    local_state = root / "Local State"
    if not local_state.exists():
        return True
    try:
        import json

        data = json.loads(local_state.read_text(encoding="utf-8"))
        os_crypt = data.get("os_crypt") or {}
        return "app_bound_encrypted_key" not in os_crypt
    except Exception:
        return True


def _browser_root(name: str) -> Path | None:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    roots = {
        "chrome": local / "Google" / "Chrome" / "User Data",
        "edge": local / "Microsoft" / "Edge" / "User Data",
        "chromium": local / "Chromium" / "User Data",
        "brave": local / "BraveSoftware" / "Brave-Browser" / "User Data",
        "opera": appdata / "Opera Software" / "Opera Stable",
        "vivaldi": local / "Vivaldi" / "User Data",
    }
    return roots.get(name)


def detect_browsers() -> list[tuple[str, str]]:
    """Возвращает список (имя для yt-dlp, профиль) для браузеров с профилем на этом ПК."""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))

    known = {
        "chrome": (local / "Google" / "Chrome" / "User Data", "Default"),
        "edge": (local / "Microsoft" / "Edge" / "User Data", "Default"),
        "chromium": (local / "Chromium" / "User Data", "Default"),
        "brave": (local / "BraveSoftware" / "Brave-Browser" / "User Data", "Default"),
        "opera": (appdata / "Opera Software" / "Opera Stable", "Default"),
        "vivaldi": (local / "Vivaldi" / "User Data" / "Default", "Default"),
        "firefox": (appdata / "Mozilla" / "Firefox" / "Profiles", None),
        "thunderbird": (appdata / "Thunderbird" / "Profiles", None),
    }

    found: list[tuple[str, str]] = []
    for name, (root, profile) in known.items():
        if not root.exists():
            continue
        if profile is None:
            subs = [p for p in root.iterdir() if p.is_dir()] if root.is_dir() else []
            if not subs:
                continue
            found.append((name, str(subs[0])))
        else:
            if (root / profile).exists():
                found.append((name, str(root / profile)))
    return found


def is_browser_running(names: list[str]) -> list[str]:
    """Какие из процессов браузеров сейчас запущены."""
    import subprocess as sp

    running = []
    try:
        out = sp.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.lower()
    except Exception:
        return running
    for name in names:
        if f'"{name}.exe"' in out:
            running.append(name)
    return running


def get_default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    downloads.mkdir(exist_ok=True)
    return downloads


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_root() -> Path:
    """Папка с компонентами и журналами (не требует прав администратора)."""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
    return base / "Media-Downloader"


def tools_root() -> Path:
    return data_root() / "tools"


def pylibs_dir() -> Path:
    """Сюда распаковываются python-пакеты (yt-dlp), скачанные при запуске."""
    return tools_root() / "pylibs"


def deno_dir() -> Path:
    return tools_root() / "deno"


def node_dir() -> Path:
    return tools_root() / "node"


def git_dir() -> Path:
    return tools_root() / "git"


def ffmpeg_cache_dir() -> Path:
    return tools_root() / "ffmpeg"


def _tool_candidates(tool: str) -> list[Path]:
    names = (f"{tool}.exe", tool)
    bundle = Path(getattr(sys, "_MEIPASS", app_dir()))
    roots = [bundle, app_dir(), ffmpeg_cache_dir(), Path.cwd()]
    seen = set()
    out: list[Path] = []
    for root in roots:
        for rel in ("", "ffmpeg", "ffmpeg/bin", "bin"):
            for name in names:
                p = (root / rel / name) if rel else (root / name)
                key = str(p).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(p)
    return out


def _which(name: str) -> str | None:
    # Возвращаем ПОЛНЫЙ путь: иначе dirname("ffmpeg.exe") даёт "." и yt-dlp
    # не находит FFmpeg при склейке видео+аудио.
    try:
        found = shutil.which(name)
        if not found:
            return None
        if tool_works(found):
            return found
    except Exception:
        pass
    return None


_WORKING_TOOLS: set[str] = set()


def tool_works(path: str | Path) -> bool:
    """Проверяет, что программа запускается (для shared-FFmpeg нужны DLL рядом)."""
    key = str(path).lower() if os.name == "nt" else str(path)
    if key in _WORKING_TOOLS:
        return True
    try:
        result = subprocess.run(
            [str(path), "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except Exception:
        return False
    if result.returncode == 0:
        _WORKING_TOOLS.add(key)
        return True
    return False


def _find_tool(tool: str) -> str | None:
    for c in _tool_candidates(tool):
        if c.exists() and c.is_file():
            return str(c.resolve())
    return _which(f"{tool}.exe") or _which(tool)


def find_ffmpeg() -> str | None:
    return _find_tool("ffmpeg")


def find_ffprobe() -> str | None:
    return _find_tool("ffprobe")


def get_ffmpeg_download() -> dict | None:
    try:
        url = "https://api.github.com/repos/yt-dlp/FFmpeg-Builds/releases/latest"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Media-Downloader"})
        r.raise_for_status()
        data = r.json()
        # shared-сборка весит вдвое меньше статической (~88 МБ против ~192 МБ).
        # Рядом с ffmpeg.exe распаковываются её DLL, поэтому она работает так же.
        preferred = (
            "ffmpeg-master-latest-win64-gpl-shared.zip",
            "ffmpeg-master-latest-win64-gpl.zip",
        )
        assets = {a.get("name"): a for a in data.get("assets", [])}
        for name in preferred:
            if name in assets:
                a = assets[name]
                digest = a.get("digest")
                if not digest:
                    digest = _ffmpeg_checksum(data, name)
                return {
                    "name": a["name"],
                    "url": a["browser_download_url"],
                    "size": a.get("size", 0),
                    "digest": digest,
                    "release_url": data.get("html_url", ""),
                }
    except Exception:
        LOGGER.warning("Could not fetch FFmpeg release metadata", exc_info=True)
    return None


def _ffmpeg_checksum(release: dict, filename: str) -> str | None:
    """Запасной путь: берёт SHA-256 из checksums.sha256, если API не отдал digest."""
    checksums = next((a for a in release.get("assets", [])
                      if a.get("name") == "checksums.sha256"), None)
    if not checksums or not checksums.get("browser_download_url"):
        return None
    try:
        response = requests.get(checksums["browser_download_url"], timeout=10,
                                headers={"User-Agent": "Media-Downloader"})
        response.raise_for_status()
        for line in response.text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
                return "sha256:" + parts[0].lower()
    except Exception:
        LOGGER.warning("Could not read FFmpeg checksums", exc_info=True)
    return None


def format_bytes(size: int) -> str:
    if size <= 0:
        return "0 Б"
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    i = 0
    size = float(size)
    while size >= 1024 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    if i == 0:
        return f"{int(size)} Б"
    return f"{size:.1f} {units[i]}"


def format_duration(seconds) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def format_eta(seconds) -> str:
    if not seconds or seconds <= 0:
        return "—"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


_LOGIN_COOKIE_NAMES = {"SID", "__Secure-1PSID", "LOGIN_INFO"}


def check_cookies_file(path) -> dict:
    """Проверяет, что cookies.txt содержит данные входа в аккаунт YouTube.

    Многие расширения экспортируют только «гостевые» cookies (например,
    ``__Secure-3PSID`` без ``SID``/``LOGIN_INFO``). Такой файл выглядит
    нормально, но YouTube считает вас неавторизованным — приватные,
    возрастные и часть эфиров не открываются с ошибкой «Sign in to confirm
    you're not a bot».
    """
    import http.cookiejar as cj

    try:
        jar = cj.MozillaCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return {
            "ok": False,
            "total": 0,
            "authenticated": False,
            "detail": "Не удалось прочитать файл cookies. Проверьте формат (Netscape .txt).",
        }

    names = {c.name for c in jar}
    if not names:
        return {
            "ok": False,
            "total": 0,
            "authenticated": False,
            "detail": "Файл cookies пуст.",
        }

    if names & _LOGIN_COOKIE_NAMES:
        return {
            "ok": True,
            "total": len(names),
            "authenticated": True,
            "detail": f"Cookies загружены ({len(names)} шт.), вход в аккаунт подтверждён.",
        }

    return {
        "ok": True,
        "total": len(names),
        "authenticated": False,
        "detail": (
            f"В файле {len(names)} cookies, но НЕТ данных входа (SID / LOGIN_INFO).\n"
            "YouTube будет считать вас гостем: приватные, возрастные видео и часть "
            "эфиров не скачаются («Sign in to confirm you're not a bot»).\n"
            "Экспортируйте cookies заново, находясь авторизованным на YouTube."
        ),
    }


def _tls_ok(ip: str, sni: str, timeout: float) -> bool:
    """Проверяет, проходит ли TLS-рукопожатие к ip с указанным SNI."""
    import socket
    import ssl

    try:
        sock = socket.create_connection((ip, 443), timeout=timeout)
    except Exception:
        return False
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        ctx.wrap_socket(sock, server_hostname=sni)
        return True
    except Exception:
        return False
    finally:
        try:
            sock.close()
        except Exception:
            pass


def check_youtube_network(timeout: float = 8.0) -> dict:
    """Диагностика доступа к YouTube.

    Различает обычные сбои и блокировку по SNI (DPI у провайдера): тогда
    TLS к YouTube не проходит, а к google.com через тот же IP — проходит.
    """
    import socket

    yt_host = "www.youtube.com"
    probe_host = "www.google.com"

    try:
        yt_ip = socket.gethostbyname(yt_host)
    except Exception:
        return {
            "state": "dns",
            "detail": f"DNS не может найти {yt_host}. Похоже на проблему с интернетом или DNS-сервером.",
        }

    try:
        sock = socket.create_connection((yt_ip, 443), timeout=timeout)
        sock.close()
    except Exception:
        return {
            "state": "tcp",
            "detail": f"Порт 443 у {yt_ip} недоступен. Соединение блокируется до начала шифрования.",
        }

    if _tls_ok(yt_ip, yt_host, timeout):
        return {"state": "ok", "detail": f"YouTube доступен ({yt_ip})."}

    if _tls_ok(yt_ip, probe_host, timeout):
        return {
            "state": "blocked",
            "detail": (
                f"Провайдер блокирует YouTube по имени домена (SNI/DPI).\n"
                f"Один и тот же IP {yt_ip} открывается как «{probe_host}», "
                f"но не открывается как «{yt_host}» — значит режется именно имя сайта.\n\n"
                "Это блокировка на стороне вашей сети, а не в программе.\n"
                "Помогает только VPN или прокси — тогда YouTube качается нормально."
            ),
        }

    return {
        "state": "tls",
        "detail": (
            f"TLS-рукопожатие к {yt_ip} не проходит ни с одним именем.\n"
            "Похоже на общий сбой канала или блокировку шифрованного трафика."
        ),
    }


def looks_like_url(text: str) -> bool:
    """Грубая проверка, похож ли текст на ссылку (одно слово без пробелов)."""
    text = (text or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return False
    return bool(re.match(r"^(https?://)?[\w\-]+(\.[\w\-]+)+(/\S*)?$", text, re.I))


def get_installed_ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__

        return __version__
    except Exception:
        return ""


def get_latest_pypi_version(package: str = "yt-dlp", timeout: float = 10.0) -> str | None:
    """Последняя версия пакета на PyPI (None, если сети нет)."""
    try:
        r = requests.get(
            f"https://pypi.org/pypi/{package}/json",
            timeout=timeout,
            headers={"User-Agent": "Media-Downloader"},
        )
        r.raise_for_status()
        return (r.json().get("info") or {}).get("version")
    except Exception:
        LOGGER.warning("Could not check package update: %s", package, exc_info=True)
        return None


def version_key(version: str) -> tuple:
    """Приводит версию вида 2026.08.19 к кортежу чисел для сравнения."""
    out: list[int] = []
    for part in re.split(r"[.\-_+]", (version or "").strip()):
        m = re.match(r"(\d+)", part)
        out.append(int(m.group(1)) if m else 0)
    while len(out) < 4:
        out.append(0)
    return tuple(out[:4])


def is_newer_version(candidate: str, current: str) -> bool:
    """True, если candidate новее current."""
    if not candidate or not current:
        return False
    try:
        return version_key(candidate) > version_key(current)
    except Exception:
        return False


def can_self_update() -> bool:
    """Можно ли обновлять пакеты (программа запущена из исходников, а не .exe)."""
    return not getattr(sys, "frozen", False)


def ytdlp_update_command() -> list[str] | None:
    if not can_self_update():
        return None
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        "yt-dlp",
        "yt-dlp-ejs",
    ]


APP_REPO = "TTsanchez/media-downloader"
APP_EXE_NAME = "Media-Downloader.exe"


def get_app_version() -> str:
    try:
        if getattr(sys, "frozen", False):
            base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        else:
            base = Path(__file__).parent
        vfile = base / "version.txt"
        if vfile.exists():
            return vfile.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return "0.0.0"


def get_latest_app_release(timeout: int = 15) -> Optional[dict]:
    try:
        import requests

        url = f"https://api.github.com/repos/{APP_REPO}/releases/latest"
        headers = {"User-Agent": "Media-Downloader", "Accept": "application/vnd.github+json"}
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        LOGGER.warning("Could not check application update", exc_info=True)
        return None


def get_app_asset(release: dict, asset_name: str = APP_EXE_NAME) -> Optional[dict]:
    try:
        for asset in release.get("assets", []) or []:
            if asset.get("name") == asset_name:
                return asset
    except Exception:
        pass
    return None


def get_app_asset_url(release: dict, asset_name: str = APP_EXE_NAME) -> Optional[str]:
    asset = get_app_asset(release, asset_name)
    return asset.get("browser_download_url") if asset else None


def is_newer_app_version(remote_tag: str, local_ver: str) -> bool:
    if not remote_tag or not local_ver:
        return False
    try:
        from packaging.version import Version

        rv = remote_tag.lstrip("vV").strip()
        lv = local_ver.lstrip("vV").strip()
        if not rv:
            return False
        return Version(rv) > Version(lv)
    except Exception:
        def _v(s: str):
            parts = re.findall(r"\d+", s)
            while len(parts) < 3:
                parts.append("0")
            return tuple(int(x) for x in parts[:3])

        rv = _v(remote_tag)
        lv = _v(local_ver)
        return rv > lv


def download_file(url: str, dest: Path, timeout: int = 600,
                  expected_sha256: str | None = None) -> bool:
    """Скачивает файл с докачкой; при ошибке существующий файл не трогает."""
    import net

    try:
        LOGGER.info("File download started: url=%s destination=%s", url, dest)
        net.download(url, dest, timeout=(20, timeout), expected_sha256=expected_sha256)
        LOGGER.info("File download completed: destination=%s", dest)
        return True
    except Exception:
        LOGGER.exception("File download failed: url=%s destination=%s", url, dest)
        return False


def create_updater_script(new_exe: Path, current_exe: Path) -> Optional[Path]:
    try:
        bat = current_exe.parent / "Media-Downloader-update.bat"
        diagnostic = get_diagnostics()
        logs = diagnostic.directory if diagnostic and diagnostic.directory else ffmpeg_cache_dir().parent.parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        update_log = logs / f"media-downloader-updater-{diagnostic.session_id if diagnostic else os.getpid()}.log"
        content = f"""@echo off
chcp 65001 > nul
set "UPDATE_LOG={str(update_log).replace('%', '%%')}"
>>"%UPDATE_LOG%" echo [updater] Starting update
timeout /t 2 /nobreak > nul
:wait
tasklist /FI "IMAGENAME eq {current_exe.name}" 2>NUL | find /I "{current_exe.name}" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak > nul
    goto wait
)
move /Y "{new_exe}" "{current_exe}" >>"%UPDATE_LOG%" 2>&1
if errorlevel 1 (
    >>"%UPDATE_LOG%" echo [updater] ERROR: Could not replace the executable
    exit /b 1
)
start "" "{current_exe}" >>"%UPDATE_LOG%" 2>&1
if errorlevel 1 (
    >>"%UPDATE_LOG%" echo [updater] ERROR: Could not launch the updated application
    exit /b 1
)
>>"%UPDATE_LOG%" echo [updater] Update completed
del "%~f0" > nul
"""
        bat.write_text(content, encoding="utf-8")
        return bat
    except Exception:
        LOGGER.exception("Could not create updater script")
        return None

