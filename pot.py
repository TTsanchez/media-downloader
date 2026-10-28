"""Управление PO Token provider (bgutil) и поиск Deno.

YouTube с 2025 требует PO Token для большинства запросов. Без него yt-dlp
получает «The page needs to be reloaded». Токены генерирует bgutil-провайдер,
который представляет собой Node.js-приложение (HTTP-сервер на порту 4416).
"""

import os
import logging
import socket
import subprocess
import sys
import threading
from pathlib import Path
from diagnostics import redact, run_process
from utils import deno_dir, git_dir, node_dir

LOGGER = logging.getLogger("media_downloader.pot")

PROVIDER_VERSION = "2.0.2"
PROVIDER_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
PROVIDER_URL = f"{PROVIDER_REPO}#v{PROVIDER_VERSION}"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 4416
SERVER_PING_PATH = "/ping"

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Процесс запущенного нами сервера, чтобы корректно остановить его при выходе
_proc: "subprocess.Popen | None" = None
_server_lock = threading.Lock()


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def provider_dir() -> Path:
    # Не кладём 165 МБ рядом с .exe: папка может быть в OneDrive/Program Files.
    # Используем локальное хранилище пользователя.
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    new = Path(base) / "Media-Downloader" / "pot"
    legacy = Path(base) / "YT-Downloader" / "pot"
    # Совместимость: если провайдер уже стоял под старым именем — не качаем заново.
    if not new.exists() and legacy.exists():
        return legacy
    return new


def server_dir() -> Path:
    return provider_dir() / "server"


def server_main() -> Path:
    return server_dir() / "build" / "main.js"


def provider_installed() -> bool:
    return server_main().exists() and (server_dir() / "node_modules").exists()


def provider_version() -> str:
    """Версия собранного провайдера (из server/package.json)."""
    import json

    try:
        data = json.loads((server_dir() / "package.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "")
    except Exception:
        return ""


def find_deno() -> str | None:
    # Возвращаем ПОЛНЫЙ путь, а не просто имя: в собранном .exe рабочий каталог
    # и PATH могут отличаться, и yt-dlp не сможет запустить "deno.exe".
    import shutil

    managed = deno_dir() / ("deno.exe" if os.name == "nt" else "deno")
    if managed.is_file():
        return str(managed)

    for name in ("deno.exe", "deno"):
        full = shutil.which(name)
        if not full:
            continue
        try:
            r = subprocess.run(
                [full, "--version"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                return full
        except Exception:
            pass

    home = Path.home()
    candidates = [
        home / ".deno" / "bin" / "deno.exe",
        home / ".deno" / "bin" / "deno",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "deno" / "deno.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if packages.is_dir():
        for entry in packages.iterdir():
            if entry.is_dir() and entry.name.lower().startswith("denoland.deno"):
                for cand in (entry / "deno.exe", entry / "Links" / "deno.exe"):
                    if cand.exists():
                        return str(cand)

    links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "deno.exe"
    if links.exists():
        return str(links)

    return None


def find_node() -> str | None:
    import shutil

    managed = node_dir() / ("node.exe" if os.name == "nt" else "node")
    if managed.is_file():
        return str(managed)

    for name in ("node.exe", "node"):
        full = shutil.which(name)
        if not full:
            continue
        try:
            r = subprocess.run(
                [full, "--version"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                return full
        except Exception:
            pass
    return None


def tool_environment() -> dict:
    """PATH с нашими портативными Node.js, Git и Deno (для npm и сборки)."""
    env = dict(os.environ)
    extra: list[str] = []
    for folder, relative in ((node_dir(), ""), (git_dir(), "cmd"), (git_dir(), "bin"), (deno_dir(), "")):
        candidate = folder / relative if relative else folder
        if candidate.is_dir():
            extra.append(str(candidate))
    if extra:
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def _port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex((SERVER_HOST, SERVER_PORT)) == 0


def server_running() -> bool:
    return _port_open()


def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    return run_process(cmd, cwd=cwd, timeout=timeout, env=tool_environment())


def install_provider(log=None, force: bool = False) -> tuple[bool, str]:
    """Клонирует и собирает bgutil-провайдер. Блокирующий вызов."""
    def emit(msg: str):
        if log:
            log(msg)

    git = _find_git()
    if not git:
        return False, "Не найден git. Нажмите «Установить всё» в настройках."

    node = find_node()
    if not node:
        return False, "Не найден Node.js (нужен версии 20+). Нажмите «Установить всё» в настройках."

    npm = _find_npm()
    if not npm:
        return False, "Не найден npm. Переустановите Node.js."

    provider_dir().mkdir(parents=True, exist_ok=True)

    if server_dir().exists() and force:
        emit("Удаляю предыдущую сборку провайдера...")
        import shutil

        shutil.rmtree(server_dir(), ignore_errors=True)

    if not server_dir().exists():
        if (provider_dir() / ".git").exists():
            emit("Обновляю исходники провайдера...")
            _run([git, "-C", str(provider_dir()), "fetch", "--depth", "1", "origin",
                  f"refs/tags/{PROVIDER_VERSION}:refs/tags/{PROVIDER_VERSION}"])
            _run([git, "-C", str(provider_dir()), "checkout", "--force", PROVIDER_VERSION])
        else:
            # Неполный клон от прошлой неудачной попытки мешает git clone
            if any(provider_dir().iterdir()):
                import shutil

                shutil.rmtree(provider_dir(), ignore_errors=True)
                provider_dir().mkdir(parents=True, exist_ok=True)
            emit(f"Клонирую PO Token провайдер (версия {PROVIDER_VERSION})...")
            code, out = _run([git, "clone", "--depth", "1", "--branch", PROVIDER_VERSION,
                              PROVIDER_REPO, str(provider_dir())])
            if code != 0:
                return False, f"Не удалось склонировать провайдер:\n{out[-400:]}"
    if not server_dir().exists():
        return False, "В исходниках провайдера нет папки server."

    if not (server_dir() / "node_modules").exists():
        emit("Ставлю зависимости Node.js (это долго, ~2-5 минут)...")
        code, out = _run([npm, "ci"], cwd=server_dir())
        if code != 0:
            code, out2 = _run([npm, "install"], cwd=server_dir())
            out = out + "\n" + out2
            if code != 0:
                return False, f"npm ci не удался:\n{out[-400:]}"

    if not server_main().exists():
        emit("Собираю провайдер...")
        for cmd in (
            [node, "node_modules/typescript/bin/tsc"],
            [_find_npx() or "npx.cmd", "tsc"],
            ["npx", "tsc"],
        ):
            code, out = _run(cmd, cwd=server_dir())
            if code == 0 and server_main().exists():
                break
        else:
            return False, f"Сборка не удалась:\n{out[-400:]}"

    emit("Провайдер готов.")
    return True, str(server_main())


def _find_git() -> str | None:
    managed = git_dir() / "cmd" / ("git.exe" if os.name == "nt" else "git")
    if managed.is_file():
        return str(managed)
    managed = git_dir() / "bin" / ("git.exe" if os.name == "nt" else "git")
    if managed.is_file():
        return str(managed)

    import shutil

    for name in ("git.exe", "git"):
        full = shutil.which(name)
        if not full:
            continue
        try:
            r = subprocess.run(
                [full, "--version"],
                capture_output=True,
                timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                return full
        except Exception:
            pass
    return None


def _find_npm() -> str | None:
    managed = node_dir() / ("npm.cmd" if os.name == "nt" else "npm")
    if managed.is_file():
        return str(managed)

    import shutil

    for name in ("npm.cmd", "npm"):
        full = shutil.which(name)
        if not full:
            continue
        try:
            r = subprocess.run(
                [full, "--version"],
                capture_output=True,
                timeout=20,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                return full
        except Exception:
            pass
    return None


def _find_npx() -> str | None:
    managed = node_dir() / ("npx.cmd" if os.name == "nt" else "npx")
    return str(managed) if managed.is_file() else None


def start_server(log=None) -> bool:
    with _server_lock:
        return _start_server(log)


def _start_server(log=None) -> bool:
    """Запускает PO Token сервер. Возвращает True, если он отвечает."""
    global _proc
    if server_running():
        return True
    if not provider_installed():
        return False

    node = find_node()
    if not node:
        return False

    if log:
        log(f"Запускаю PO Token сервер ({SERVER_HOST}:{SERVER_PORT})...")

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)

    try:
        LOGGER.info("Starting PO Token server: node=%s script=%s", node, server_main())
        _proc = subprocess.Popen(
            [node, str(server_main())],
            cwd=str(server_dir()),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        threading.Thread(target=_capture_server_output, args=(_proc,), name="PO-token-output", daemon=True).start()
    except Exception as e:
        LOGGER.exception("Could not start PO Token server")
        if log:
            log(f"Не удалось запустить PO Token сервер: {e}")
        return False

    import time

    for _ in range(30):
        if server_running():
            LOGGER.info("PO Token server is ready")
            if log:
                log("PO Token сервер запущен.")
            return True
        if _proc is None or _proc.poll() is not None:
            LOGGER.error("PO Token server exited before becoming ready")
            return False
        time.sleep(1)

    if log:
        log("PO Token сервер не ответил за 30 секунд.")
    LOGGER.error("PO Token server startup timed out")
    return False


def _capture_server_output(proc):
    try:
        with proc.stdout:
            for line in proc.stdout:
                LOGGER.info("PO Token: %s", redact(line.rstrip())[:12000])
        code = proc.wait()
        LOGGER.log(logging.ERROR if proc is _proc else logging.INFO,
                   "PO Token process exited: pid=%s rc=%s", proc.pid, code)
    except Exception:
        LOGGER.exception("Could not capture PO Token output")


def _server_pids() -> list[str]:
    """PID-ы процессов, слушающих порт провайдера (без учёта языка ОС)."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return []

    pids: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        # Proto Local Foreign State PID — состояние локализовано, PID всегда последний
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if not parts[1].endswith(f":{SERVER_PORT}"):
            continue
        pid = parts[-1]
        if pid.isdigit() and pid not in pids:
            pids.append(pid)
    return pids


def stop_server() -> None:
    global _proc
    with _server_lock:
        proc, _proc = _proc, None
        if proc is None or proc.poll() is not None:
            return
        LOGGER.info("Stopping owned PO Token process: pid=%s", proc.pid)
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
                )
            else:
                proc.terminate()
                proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.warning("Normal PO Token shutdown failed; killing process", exc_info=True)
            proc.kill()
