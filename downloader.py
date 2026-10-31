import os
import logging
from copy import copy
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from utils import find_ffmpeg
from pot import find_deno, server_running, start_server
from diagnostics import register_cookie_secrets, register_secret, report_exception


class YtDlpLogger:
    def __init__(self, logger):
        self.logger = logger

    def debug(self, message):
        self.logger.debug("yt-dlp: %s", message)

    def warning(self, message):
        self.logger.warning("yt-dlp: %s", message)

    def error(self, message):
        self.logger.error("yt-dlp: %s", message)

# Порядок попыток получить видео с YouTube. В каждой попытке — свой клиент:
# несколько клиентов в одном запросе чаще упираются в проверку «Sign in to
# confirm you're not a bot», чем один анонимный.
#
# visionos идёт первым намеренно. Это тот же клиент, который yt-dlp выбирает
# сам, когда рядом нет JS-рантайма (см. _DEFAULT_JSLESS_CLIENTS): он отдаёт 1080p
# со звуком без cookies, без PO Token и без проверок «вы не робот». Как только
# в PATH появляется Deno, дефолтом становится набор tv_samsung + visionos + web,
# и именно web чаще всего требует cookies — поэтому дефолты оставлены на потом.
# android_vr — запасной клиент для трансляций, web — последняя попытка.
CLIENT_FALLBACKS: list[list[str] | None] = [
    ["visionos"],                  # анонимный клиент: 1080p со звуком без cookies и PO Token
    ["tv_samsung"],                # второй анонимный клиент, тоже отдаёт 1080p
    ["default"],                   # полный набор yt-dlp (tv_samsung + visionos + web)
    ["android_vr"],                # невысокое качество, зато без cookies
    ["web"],                       # последняя попытка: обычно нужен PO Token или cookies
]

# Ошибки, при которых имеет смысл повторить с другим клиентом.
RETRYABLE_MARKERS = (
    "page needs to be reloaded",
    "sign in to confirm",
    "no video formats found",
    "requested format is not available",
    "only images are available",
    "unable to download api page",
    "failed to resolve",
    "getaddrinfo",
    "name resolution",
    "connection reset",
    "connection aborted",
    "connection refused",
    "timed out",
    "temporary failure",
    "http error 403",
    "http error 5",
    "unable to connect",
)

MAX_ATTEMPTS = len(CLIENT_FALLBACKS)


def build_runtime_opts(clients: list[str] | None = None, proxy: str | None = None) -> dict:
    """Опции, нужные для текущего состояния YouTube (PO token, JS-челлендж).

    clients=None означает «не задавать player_client вообще» — тогда yt-dlp
    использует свои дефолты и сам подбирает клиенты (без логина, с куками,
    без JS-рантайма).
    """
    opts: dict[str, Any] = {}

    if proxy:
        register_secret(proxy)
        opts["proxy"] = proxy

    deno = find_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}

    args: dict[str, dict[str, list[str]]] = {}
    if clients is not None:
        args["youtube"] = {"player_client": list(clients)}
    if server_running():
        args["youtubepot-bgutilhttp"] = {"base_url": ["http://127.0.0.1:4416"]}
    if args:
        opts["extractor_args"] = args
    return opts


def ensure_pot_server() -> bool:
    """Поднимает PO Token сервер, если он установлен, но упал."""
    if server_running():
        return True
    return start_server()


class DownloadProgress:
    def __init__(self):
        self.status = "idle"
        self.percent = 0.0
        self.speed = 0.0
        self.eta = 0
        self.downloaded = 0
        self.total = 0
        self.filename = ""
        self.error = ""
        self.current_index = 0
        self.total_count = 0
        self.is_playlist = False
        self.retry_message = ""


class MediaDownloader:
    def __init__(self, progress_callback: Callable[[DownloadProgress], None] | None = None):
        self.progress_callback = progress_callback
        self.progress = DownloadProgress()
        self._stop_event = threading.Event()
        self._ydl = None
        self._url = ""
        self.job_id = uuid.uuid4().hex[:12]
        self.logger = logging.LoggerAdapter(logging.getLogger("media_downloader.downloader"), {"job": self.job_id})
        self._last_log_state = None

    def _emit(self):
        state = (self.progress.status, int(self.progress.percent // 10))
        if state != self._last_log_state:
            self._last_log_state = state
            self.logger.info("Progress status=%s percent=%.1f bytes=%s/%s speed=%s eta=%s playlist=%s/%s",
                             self.progress.status, self.progress.percent, self.progress.downloaded,
                             self.progress.total, self.progress.speed, self.progress.eta,
                             self.progress.current_index, self.progress.total_count)
            if self.progress.status == "error":
                self.logger.error("Job failed: %s", self.progress.error)
        if self.progress_callback:
            try:
                self.progress_callback(copy(self.progress))
            except Exception:
                report_exception("Ошибка обновления прогресса загрузки")

    def stop(self):
        self.logger.info("Cancellation requested")
        self._stop_event.set()

    def extract_info(
        self,
        url: str,
        cookies_file: str | None = None,
        cookies_from_browser: str | None = None,
        proxy: str | None = None,
    ) -> Dict[str, Any]:
        register_cookie_secrets(cookies_file)
        self.logger.info("Extract info started: url=%s cookies_file=%s browser=%s proxy_enabled=%s",
                         url, bool(cookies_file), cookies_from_browser, bool(proxy))
        base_opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "logger": YtDlpLogger(self.logger),
            "noplaylist": False,
            "noprogress": True,
            "socket_timeout": 30,
        }
        if cookies_file:
            base_opts["cookiefile"] = cookies_file
        if cookies_from_browser:
            base_opts["cookiesfrombrowser"] = (cookies_from_browser,)
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            base_opts["ffmpeg_location"] = ffmpeg

        last_error = ""

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self._stop_event.is_set():
                raise RuntimeError("Отменено")
            opts = dict(base_opts)
            self.logger.info("Extract attempt=%s/%s clients=%s", attempt, MAX_ATTEMPTS, CLIENT_FALLBACKS[attempt - 1])
            ensure_pot_server()
            opts.update(build_runtime_opts(clients=CLIENT_FALLBACKS[attempt - 1], proxy=proxy))
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if not info:
                    last_error = "YouTube не вернул данные по этой ссылке"
                    if attempt < MAX_ATTEMPTS:
                        if self._stop_event.wait(2 * attempt):
                            raise RuntimeError("Отменено")
                        continue
                    break

                # Без явного "format" info['height'] отражает произвольный
                # формат, а не лучший. Считаем максимум по всем форматам,
                # иначе в GUI показывается 360p вместо 1080p.
                fmts = info.get("formats") or []
                vids = [f for f in fmts if f.get("vcodec") not in (None, "none")]
                heights = [f["height"] for f in vids if f.get("height")]
                if heights:
                    info["height"] = max(heights)
                elif not info.get("height") and vids:
                    info["height"] = None
                self.logger.info("Extract info completed: playlist=%s height=%s formats=%s",
                                 "entries" in info, info.get("height"), len(fmts))
                return info
            except (DownloadError, ExtractorError) as e:
                self.logger.warning("Extract attempt failed", exc_info=True)
                last_error = self._clean_error(str(e))
            except Exception as e:
                self.logger.warning("Extract attempt failed unexpectedly", exc_info=True)
                last_error = self._clean_error(str(e))

            if attempt < MAX_ATTEMPTS and self._should_retry(last_error):
                if self._stop_event.wait(2 * attempt):
                    raise RuntimeError("Отменено")
                continue
            break

        raise RuntimeError(last_error or "Не удалось получить информацию")

    @staticmethod
    def _clean_error(msg: str) -> str:
        cleaned = "\n".join(line for line in msg.splitlines() if line.strip() and not line.strip().startswith("["))
        return cleaned.strip() or "Неизвестная ошибка"

    def download(
        self,
        url: str,
        output_dir: Path,
        mode: str = "best",
        custom_format: str | None = None,
        custom_audio_only: bool = False,
        audio_format: str = "mp3",
        audio_quality: str = "192",
        playlist_start: int = 1,
        playlist_end: int = 0,
        cookies_file: str | None = None,
        cookies_from_browser: str | None = None,
        proxy: str | None = None,
        restrict_filenames: bool = True,
        download_archive: str | None = None,
    ):
        register_cookie_secrets(cookies_file)
        self.logger.info("Download started: url=%s mode=%s format=%s audio=%s quality=%s playlist=%s:%s "
                         "cookies_file=%s browser=%s proxy_enabled=%s archive_enabled=%s output=%s",
                         url, mode, custom_format, audio_format, audio_quality, playlist_start, playlist_end,
                         bool(cookies_file), cookies_from_browser, bool(proxy), bool(download_archive), output_dir)
        self.progress = DownloadProgress()
        self._url = url
        self.progress.status = "extracting"
        self._emit()

        if self._stop_event.is_set():
            self.progress.status = "cancelled"
            self._emit()
            return

        try:
            if playlist_start < 1 or playlist_end < 0 or (playlist_end and playlist_end < playlist_start):
                raise ValueError("Проверьте диапазон плейлиста: конец должен быть не меньше начала.")
            output_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as e:
            self.logger.exception("Download preparation failed")
            self.progress.status = "error"
            self.progress.error = str(e)
            self._emit()
            return

        title_template = "%(title).60s" if restrict_filenames else "%(title)s"
        outtmpl = str(output_dir / f"{title_template} [%(id)s].%(ext)s")

        opts: Dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "logger": YtDlpLogger(self.logger),
            "noplaylist": False,
            "outtmpl": outtmpl,
            "progress_hooks": [self._hook],
            "noprogress": True,
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            # 8 потоков — как в ydl-studio: заметно быстрее на HLS и трансляциях.
            "concurrent_fragment_downloads": 8,
            # http_chunk_size намеренно не задан: с ним yt-dlp переоткрывает
            # соединение на каждый мегабайт, и на быстром канале загрузка
            # становится в разы медленнее.
            "buffersize": 16777216,
            "socket_timeout": 30,
            "windowsfilenames": os.name == "nt",
        }

        if cookies_file:
            opts["cookiefile"] = cookies_file
        if cookies_from_browser:
            opts["cookiesfrombrowser"] = (cookies_from_browser,)

        if download_archive:
            try:
                Path(download_archive).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            opts["download_archive"] = download_archive

        ffmpeg = find_ffmpeg()
        if ffmpeg:
            opts["ffmpeg_location"] = ffmpeg
        self.logger.info("FFmpeg location: %s", ffmpeg or "not found")

        if mode == "audio":
            opts["format"] = "ba/best"
            codec = audio_format if audio_format in ("mp3", "m4a", "opus", "wav") else "mp3"
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
            if codec != "wav":
                pp["preferredquality"] = audio_quality
            opts["postprocessors"] = [pp, {"key": "FFmpegMetadata"}]
        elif mode == "video":
            # Только видео, без звуковой дорожки
            opts["format"] = "bv"
        elif mode == "custom":
            opts["format"] = custom_format or "bv*+ba/b"
        else:
            opts["format"] = "bv*+ba/b"

        # Объединяем в mp4 только когда в результате есть видео и аудио.
        if mode == "best" or (mode == "custom" and not custom_audio_only):
            opts["merge_output_format"] = "mp4"

        if playlist_end and playlist_end >= playlist_start:
            opts["playlist_items"] = f"{playlist_start}:{playlist_end}"
            opts["ignoreerrors"] = True
        elif playlist_start > 1:
            opts["playlist_items"] = f"{playlist_start}:"
            opts["ignoreerrors"] = True

        last_error = ""

        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if self._stop_event.is_set():
                    self.progress.status = "cancelled"
                    return

                attempt_opts = dict(opts)
                self.logger.info("Download attempt=%s/%s clients=%s", attempt, MAX_ATTEMPTS, CLIENT_FALLBACKS[attempt - 1])
                ensure_pot_server()
                attempt_opts.update(build_runtime_opts(clients=CLIENT_FALLBACKS[attempt - 1], proxy=proxy))

                try:
                    with YoutubeDL(attempt_opts) as ydl:
                        self._ydl = ydl
                        result = ydl.download([url])
                        if result:
                            raise RuntimeError("Не все файлы удалось скачать. Проверьте доступность видео в плейлисте.")
                    if self._stop_event.is_set():
                        self.progress.status = "cancelled"
                    else:
                        self.progress.status = "done"
                        self.progress.percent = 100.0
                        self.progress.retry_message = ""
                        self.progress.error = ""
                    return
                except (DownloadError, ExtractorError) as e:
                    if "cancel" in str(e).lower() or self._stop_event.is_set():
                        self.progress.status = "cancelled"
                        return
                    last_error = self._clean_error(str(e)) or "Ошибка загрузки"
                    self.logger.warning("Download attempt failed", exc_info=True)
                except Exception as e:
                    if self._stop_event.is_set():
                        self.progress.status = "cancelled"
                        return
                    last_error = self._clean_error(str(e)) or "Неизвестная ошибка"
                    self.logger.warning("Download attempt failed unexpectedly", exc_info=True)

                if attempt < MAX_ATTEMPTS and self._should_retry(last_error):
                    self.progress.status = "extracting"
                    self.progress.retry_message = (
                        f"Попытка {attempt} не удалась, пробую способ {attempt + 1} из {MAX_ATTEMPTS}..."
                    )
                    self.progress.error = last_error
                    self._emit()
                    # Нарастающая пауза: сеть/DNS часто отходят через несколько секунд
                    if self._stop_event.wait(2 * attempt):
                        self.progress.status = "cancelled"
                        return
                    continue
                break

            self.progress.status = "error"
            self.progress.error = last_error
        finally:
            self._ydl = None
            self._emit()

    @staticmethod
    def _should_retry(error: str) -> bool:
        low = error.lower()
        return any(marker in low for marker in RETRYABLE_MARKERS)

    def _hook(self, d: Dict[str, Any]):
        if self._stop_event.is_set():
            raise DownloadError("cancelled by user")

        status = d.get("status")
        info = d.get("info_dict") or {}
        if info:
            idx = info.get("playlist_index")
            n = info.get("playlist_count")
            if idx and n:
                self.progress.is_playlist = True
                self.progress.current_index = idx
                self.progress.total_count = n

        if d.get("filename"):
            self.progress.filename = d["filename"]

        if status == "downloading":
            self.progress.status = "downloading"
            self.progress.retry_message = ""
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            self.progress.total = total
            self.progress.downloaded = downloaded
            self.progress.percent = min(downloaded / total * 100.0, 100.0) if total else 0.0
            self.progress.speed = d.get("speed") or 0
            self.progress.eta = d.get("eta") or 0
        elif status == "finished":
            self.progress.status = "merging"
            self.progress.percent = 99.0
        elif status == "error":
            self.progress.status = "error"
            self.progress.error = str(d.get("error", "Unknown error"))

        self._emit()
