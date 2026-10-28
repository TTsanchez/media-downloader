"""Persistent, redacted diagnostics. No application or third-party imports here."""

import atexit
from collections import deque
from datetime import datetime, timezone
import faulthandler
import importlib.metadata
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit
import uuid
import zipfile


LOG = logging.getLogger("media_downloader")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False
_service = None
_service_lock = threading.RLock()
_secrets = set()
_secrets_lock = threading.Lock()
_URL = re.compile(r"(?:https?|socks[45]h?)://[^\s<>\"']+", re.I)
_HEADERS = re.compile(r"\b(authorization|proxy-authorization|cookie|set-cookie)\s*:\s*[^\r\n]+", re.I)
_VALUES = re.compile(
    r"(?i)(\b(?:password|passwd|token|po_token|access_token|refresh_token|api_key|apikey|secret|"
    r"SID|LOGIN_INFO|__Secure-[\w-]+)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)


def register_secret(value):
    if isinstance(value, str) and len(value) >= 4:
        values = {value}
        if _URL.match(value):
            try:
                password = urlsplit(value).password
                if password and len(password) >= 4:
                    values.update((password, unquote(password)))
            except ValueError:
                pass
        with _secrets_lock:
            _secrets.update(values)


def register_cookie_secrets(path):
    """Remember cookie values for redaction, never record or export the cookie file."""
    if not path:
        return
    try:
        with Path(path).open(encoding="utf-8", errors="replace") as source:
            for line in source:
                line = line.rstrip("\r\n")
                if line and (not line.startswith("#") or line.startswith("#HttpOnly_")):
                    register_secret(line)
                    fields = line.split("\t")
                    if len(fields) == 7:
                        register_secret(fields[-1])
    except (OSError, ValueError):
        LOG.debug("Cookie file could not be read for diagnostic redaction")


def redact(value):
    text = str(value)
    with _secrets_lock:
        secrets = tuple(_secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        for variant in (secret, json.dumps(secret, ensure_ascii=False)[1:-1], repr(secret)[1:-1]):
            text = text.replace(variant, "<redacted>")
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = _HEADERS.sub(lambda m: m[1] + ": <redacted>", text)
    text = _VALUES.sub(lambda m: m[1] + "<redacted>", text)
    text = re.sub(r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)\b", "<redacted>", text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=._-]+", r"\1 <redacted>", text)

    def clean_url(match):
        try:
            parsed = urlsplit(match[0])
            host = parsed.netloc.rsplit("@", 1)[-1]
            if "@" in parsed.netloc:
                host = "<redacted>@" + host
            return urlunsplit((parsed.scheme, host, parsed.path,
                               "<redacted>" if parsed.query else "",
                               "<redacted>" if parsed.fragment else ""))
        except ValueError:
            return "<redacted-url>"

    text = _URL.sub(clean_url, text)
    home = str(Path.home())
    if home not in ("/", "\\", "."):
        for path in (home, home.replace("\\", "/"), home.replace("\\", "\\\\")):
            text = re.sub(re.escape(path), "<USER_HOME>", text, flags=re.I)
    return text


class SafeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds")

    def format(self, record):
        if not hasattr(record, "job"):
            record.job = "-"
        text = redact(super().format(record))
        if len(text) > 65536:
            return text[:32768] + "\n[Long diagnostic record truncated]\n" + text[-32768:]
        return text


class SafeRotatingHandler(RotatingFileHandler):
    """A full disk must not recurse through redirected stderr into logging."""
    def handleError(self, record):
        self.storage_error = "Не удалось записать диагностический журнал (проверьте место на диске)."
        if not hasattr(self, "emergency"):
            self.emergency = deque(maxlen=500)
        try:
            self.emergency.append(self.format(record))
        except Exception:
            self.emergency.append("Could not format a diagnostic record")


class MemoryLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = deque(maxlen=500)

    def emit(self, record):
        self.lines.append(self.format(record))


class LogStream:
    encoding = "utf-8"

    def __init__(self, level):
        self.level = level
        self._local = threading.local()

    def write(self, text):
        pending = getattr(self._local, "pending", "") + text
        lines = pending.split("\n")
        self._local.pending = lines.pop()
        for line in lines:
            if line.strip():
                LOG.log(self.level, "%s", line.rstrip())
        if len(self._local.pending) > 65536:
            LOG.log(self.level, "%s [truncated]", redact(self._local.pending)[:65536])
            self._local.pending = ""
        return len(text)

    def flush(self):
        pending = getattr(self._local, "pending", "")
        self._local.pending = ""
        if pending.strip():
            LOG.log(self.level, "%s", pending)

    def isatty(self):
        return False


def runtime_info():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    try:
        version = (base / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        version = "unknown"
    packages = {}
    for name in ("yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider", "ttkbootstrap", "Pillow", "requests"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = "not installed"
        except Exception:
            packages[name] = "unavailable"
    try:
        working_directory = str(Path.cwd())
    except OSError:
        working_directory = "unavailable"
    return {"app_version": version, "python": platform.python_version(),
            "os": platform.system(), "os_release": platform.release(), "os_version": platform.version(),
            "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable, "working_directory": working_directory, "packages": packages}


class Diagnostics:
    def __init__(self, directory=None, *, install_hooks=True, enable_faults=True, notify_fatal=False,
                 max_bytes=2 * 1024 * 1024, backups=3):
        self.session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.notifications = queue.Queue(maxsize=20)
        self._context_lock = threading.RLock()
        self.context = runtime_info()
        self.context["session_id"] = self.session_id
        self.closed = False
        self._fatal = False
        self.directory = None
        self.log_file = None
        self.fault_file = None
        self._fault_handle = None
        self._owns_fault_handler = False
        self._hooks = None
        self._streams = []
        preferred = Path(directory) if directory else (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "Media-Downloader" / "logs"
        )
        self.handler = None
        failures = []
        candidates = [preferred]
        try:
            candidates.append(Path(tempfile.gettempdir()) / "Media-Downloader" / "logs")
        except OSError as error:
            failures.append(str(error))
        for candidate in candidates:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                path = candidate / f"media-downloader-{self.session_id}.log"
                handler = SafeRotatingHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
                self.directory, self.log_file, self.handler = candidate.resolve(), path.resolve(), handler
                break
            except OSError as error:
                failures.append(str(error))
        if self.handler is None:
            self.handler = MemoryLog()
        self.handler.setFormatter(SafeFormatter("%(asctime)s %(levelname)-8s pid=%(process)d thread=%(threadName)s %(name)s job=%(job)s | %(message)s"))
        LOG.setLevel(logging.DEBUG)
        LOG.addHandler(self.handler)
        if failures:
            LOG.warning("Preferred log storage unavailable; fallback=%s; errors=%s", self.directory, failures)
            self.context["storage_fallback"] = True
        if install_hooks:
            self._install_hooks(notify_fatal)
        if enable_faults and self.directory and not faulthandler.is_enabled():
            try:
                self.fault_file = self.directory / f"media-downloader-{self.session_id}-fault.log"
                self._fault_handle = self.fault_file.open("a", encoding="utf-8")
                faulthandler.enable(file=self._fault_handle, all_threads=True)
                self._owns_fault_handler = True
            except (OSError, RuntimeError):
                LOG.warning("Native crash tracing could not be enabled", exc_info=True)
        self._prune()
        LOG.info("Session started: %s", json.dumps(self.context, ensure_ascii=False))
        atexit.register(self.close)

    def _install_hooks(self, notify_fatal):
        originals = (sys.excepthook, threading.excepthook, sys.unraisablehook)

        def fatal(kind, value, traceback):
            if issubclass(kind, KeyboardInterrupt):
                originals[0](kind, value, traceback)
                return
            self.report_exception("Необработанная ошибка приложения", (kind, value, traceback), fatal=True)
            if notify_fatal and os.name == "nt":
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(None,
                        f"Приложение завершилось с ошибкой.\nЖурнал: {self.log_file}\n"
                        "После перезапуска сохраните отчёт кнопкой «Сохранить отчёт».", "Media Downloader", 0x10)
                except Exception:
                    pass

        def thread_error(args):
            if args.exc_type is SystemExit:
                return
            name = args.thread.name if args.thread else "unknown"
            self.report_exception(f"Ошибка фоновой задачи: {name}", (args.exc_type, args.exc_value, args.exc_traceback))

        def unraisable(args):
            self.report_exception("Ошибка освобождения ресурсов", (args.exc_type, args.exc_value, args.exc_traceback))

        self._hooks = (originals, (fatal, thread_error, unraisable))
        sys.excepthook, threading.excepthook, sys.unraisablehook = self._hooks[1]
        for name, level in (("stdout", logging.INFO), ("stderr", logging.ERROR)):
            if getattr(sys, name) is None:
                stream = LogStream(level)
                setattr(sys, name, stream)
                self._streams.append((name, stream))

    def report_exception(self, title, exc_info, *, fatal=False):
        self._fatal = self._fatal or fatal
        LOG.log(logging.CRITICAL if fatal else logging.ERROR, "%s", title, exc_info=exc_info)
        try:
            self.notifications.put_nowait(redact(title))
        except queue.Full:
            pass

    def update_context(self, key, value):
        with self._context_lock:
            self.context[key] = value

    def _files(self):
        if self.directory is None:
            return []
        files = []
        for path in self.directory.glob("media-downloader-*.log*"):
            try:
                if path.is_file() and not path.is_symlink() and path.resolve().parent == self.directory:
                    files.append((path.stat().st_mtime, path))
            except OSError:
                continue
        return [path for _, path in sorted(files, key=lambda item: item[0], reverse=True)]

    def _prune(self):
        try:
            for path in self._files()[20:]:
                if path not in (self.log_file, self.fault_file):
                    try:
                        path.unlink()
                    except OSError:
                        pass
        except OSError:
            LOG.warning("Could not prune old diagnostics", exc_info=True)

    def export(self, destination):
        """Exports only our logs and an explicit allowlist of runtime metadata."""
        destination = Path(destination)
        if destination.suffix.lower() != ".zip":
            destination = destination.with_name(destination.name + ".zip")
        temporary = destination.with_name(destination.name + f".{uuid.uuid4().hex[:8]}.tmp")
        LOG.info("Diagnostic report requested")
        self.handler.acquire()
        try:
            self.handler.flush()
            snapshots = []
            for path in self._files()[:12]:
                try:
                    limit = 4 * 1024 * 1024
                    size = path.stat().st_size
                    with path.open("rb") as source:
                        source.seek(max(0, size - limit))
                        content = source.read(limit).decode("utf-8", errors="replace")
                    if size > limit:
                        content = "[Earlier content omitted; last 4 MiB follow]\n" + content
                    snapshots.append((path.name, redact(content)))
                except OSError:
                    continue
            if isinstance(self.handler, MemoryLog):
                snapshots.append(("memory.log", "\n".join(self.handler.lines)))
            if getattr(self.handler, "emergency", None):
                snapshots.append(("storage-error.log", "\n".join(self.handler.emergency)))
            with self._context_lock:
                context = redact(json.dumps(self.context, ensure_ascii=False, indent=2))
        finally:
            self.handler.release()
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("system.json", context)
                archive.writestr("README.txt", "Отчёт Media Downloader. Приложите описание действий перед ошибкой.\n"
                                 "В архиве только диагностические журналы и версии компонентов.\n"
                                 "Cookies, конфигурация, окружение и скачанные файлы не прикладываются.\n")
                for name, content in snapshots:
                    archive.writestr("logs/" + name, content)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def close(self):
        if self.closed:
            return
        self.closed = True
        LOG.info("Session ended after fatal error" if self._fatal else "Session ended normally")
        if self._hooks:
            originals, installed = self._hooks
            for name, original, hook in zip(("excepthook", "excepthook", "unraisablehook"), originals, installed):
                module = threading if hook is installed[1] else sys
                if getattr(module, name) is hook:
                    setattr(module, name, original)
        for name, stream in self._streams:
            if getattr(sys, name) is stream:
                setattr(sys, name, None)
        if self._owns_fault_handler:
            faulthandler.disable()
        if self._fault_handle:
            self._fault_handle.close()
        LOG.removeHandler(self.handler)
        self.handler.close()


def initialize_logging(**kwargs):
    global _service
    with _service_lock:
        if _service is None or _service.closed:
            _service = Diagnostics(**kwargs)
        return _service


def get_diagnostics():
    return _service if _service and not _service.closed else None


def report_exception(title, exc_info=None):
    info = exc_info or sys.exc_info()
    if _service and not _service.closed:
        _service.report_exception(title, info)
    else:
        LOG.error("%s", title, exc_info=info)


def run_process(cmd, cwd=None, timeout=1800, env=None):
    logger = logging.getLogger("media_downloader.process")
    started = time.monotonic()
    logger.info("Process start: command=%s cwd=%s timeout=%s env_override=%s",
                cmd, cwd, timeout, bool(env))
    try:
        result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                                text=True, errors="replace", timeout=timeout, env=env,
                                creationflags=0x08000000 if os.name == "nt" else 0)
        output = (result.stdout or "") + (result.stderr or "")
        logger.log(logging.ERROR if result.returncode else logging.DEBUG,
                   "Process finished: rc=%s duration=%.2fs output_tail=%s", result.returncode,
                   time.monotonic() - started, redact(output)[-12000:])
        return result.returncode, output
    except subprocess.TimeoutExpired as error:
        logger.exception("Process timeout after %.2fs; stdout_tail=%s stderr_tail=%s",
                         time.monotonic() - started, redact(error.stdout or "")[-4000:], redact(error.stderr or "")[-4000:])
        return 1, f"Таймаут после {timeout} с"
    except Exception as error:
        logger.exception("Process could not start")
        return 1, str(error)
