import os
import json
import logging
import subprocess
from copy import copy
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

from utils import (
    CREATE_NO_WINDOW,
    clean_media_url,
    find_ffmpeg,
    find_ffprobe,
    format_time_value,
    prepare_media_url,
    youtube_video_id,
)
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
    "нет видеодорожки",
    "нет звуковой дорожки",
)

# Клиенты, которые yt-dlp пропускает, если переданы cookies. Если запросить
# только такой клиент, форматов не останется и попытка провалится впустую.
COOKIE_UNSUPPORTED_CLIENTS = {"visionos", "android_vr"}

# Сколько раз повторять загрузку тем же клиентом, если файл фрагмента вышел
# без нужной дорожки. Повтор берёт свежие ссылки на потоки.
FRAGMENT_RETRIES = 1


def client_order(cookies_file: str | None = None, cookies_from_browser: str | None = None) -> list[list[str] | None]:
    """Порядок попыток с учётом cookies.

    С cookies yt-dlp пропускает visionos и android_vr, поэтому такие попытки
    заведомо пустые. Без cookies порядок прежний.
    """
    if not (cookies_file or cookies_from_browser):
        return list(CLIENT_FALLBACKS)
    order: list[list[str] | None] = []
    for clients in CLIENT_FALLBACKS:
        if clients is None:
            order.append(None)
            continue
        usable = [client for client in clients if client not in COOKIE_UNSUPPORTED_CLIENTS]
        if usable:
            order.append(usable)
    return order or list(CLIENT_FALLBACKS)

# Языки субтитров по умолчанию: русский и английский.
SUBTITLE_LANGS_DEFAULT = "ru,en"

# Категории SponsorBlock в том порядке, в котором они показываются в интерфейсе.
SPONSOR_CATEGORIES = (
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "filler",
    "music_offtopic",
)
# По умолчанию отмечаем только основные категории, остальные владелец включает сам.
SPONSOR_DEFAULT_CATEGORIES = ("sponsor", "selfpromo", "interaction", "intro", "outro")
SPONSOR_MODES = ("off", "mark", "remove")


def parse_subtitle_langs(value) -> list[str]:
    """Разбирает строку «ru, en» в список языков без повторов и мусора."""
    if isinstance(value, (list, tuple, set)):
        raw = [str(part) for part in value]
    else:
        raw = str(value or "").replace(";", ",").split(",")
    langs: list[str] = []
    for part in raw:
        for token in part.replace("\t", " ").split():
            token = token.strip()
            if not token:
                continue
            # Коды языков yt-dlp: en, ru, en-US, zh-Hans, ru-orig и т.п.
            if not all(ch.isalnum() or ch in "-_" for ch in token):
                continue
            if token not in langs:
                langs.append(token)
    return langs


def normalize_sponsor_categories(value) -> list[str]:
    """Оставляет только известные категории в каноническом порядке.

    None означает «по умолчанию», пустой список — «ничего не трогать».
    """
    if value is None:
        chosen = set(SPONSOR_DEFAULT_CATEGORIES)
    else:
        chosen = {str(item).strip().lower() for item in value if str(item).strip()}
    return [category for category in SPONSOR_CATEGORIES if category in chosen]


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
        self._id_hint = ""
        self._expect_video = False
        self._expect_audio = False
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
        url = prepare_media_url(clean_media_url(url))
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
        clients_order = client_order(cookies_file, cookies_from_browser)
        attempts = len(clients_order)

        for attempt in range(1, attempts + 1):
            if self._stop_event.is_set():
                raise RuntimeError("Отменено")
            opts = dict(base_opts)
            self.logger.info("Extract attempt=%s/%s clients=%s", attempt, attempts, clients_order[attempt - 1])
            ensure_pot_server()
            opts.update(build_runtime_opts(clients=clients_order[attempt - 1], proxy=proxy))
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if not info:
                    last_error = "YouTube не вернул данные по этой ссылке"
                    if attempt < attempts:
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

            if attempt < attempts and self._should_retry(last_error):
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
        embed_media: bool = True,
        subtitles: bool = False,
        sub_langs: str = SUBTITLE_LANGS_DEFAULT,
        sub_auto: bool = False,
        sub_embed: bool = True,
        sponsor_mode: str = "off",
        sponsor_categories: list[str] | None = None,
        fragment_start: float = 0.0,
        fragment_end: float | None = None,
    ):
        register_cookie_secrets(cookies_file)
        url = prepare_media_url(clean_media_url(url))
        self.logger.info("Download started: url=%s mode=%s format=%s audio=%s quality=%s playlist=%s:%s "
                         "cookies_file=%s browser=%s proxy_enabled=%s archive_enabled=%s embed_media=%s "
                         "subtitles=%s sub_langs=%s sub_auto=%s sub_embed=%s sponsor=%s sponsor_categories=%s "
                         "output=%s",
                         url, mode, custom_format, audio_format, audio_quality, playlist_start, playlist_end,
                         bool(cookies_file), cookies_from_browser, bool(proxy), bool(download_archive),
                         bool(embed_media), bool(subtitles), sub_langs, bool(sub_auto), bool(sub_embed),
                         sponsor_mode, sponsor_categories, output_dir)
        self.progress = DownloadProgress()
        self._url = url
        self._id_hint = youtube_video_id(url)
        self._expect_video = False
        self._expect_audio = False
        self.progress.status = "extracting"
        self._emit()

        if self._stop_event.is_set():
            self.progress.status = "cancelled"
            self._emit()
            return

        try:
            if playlist_start < 1 or playlist_end < 0 or (playlist_end and playlist_end < playlist_start):
                raise ValueError("Проверьте диапазон плейлиста: конец должен быть не меньше начала.")
            if fragment_start < 0 or (fragment_end is not None and fragment_end <= fragment_start):
                raise ValueError("Проверьте фрагмент: конец должен быть позже начала.")
            output_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as e:
            self.logger.exception("Download preparation failed")
            self.progress.status = "error"
            self.progress.error = str(e)
            self._emit()
            return

        title_template = "%(title).60s" if restrict_filenames else "%(title)s"
        fragment_enabled = fragment_end is not None or fragment_start > 0
        fragment_suffix = ""
        if fragment_enabled:
            end_label = format_time_value(fragment_end) if fragment_end is not None else "конец"
            fragment_suffix = f" [фрагмент {format_time_value(fragment_start)}-{end_label}]"
        outtmpl = str(output_dir / f"{title_template} [%(id)s]{fragment_suffix}.%(ext)s")

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

        if fragment_enabled:
            opts["download_ranges"] = lambda info, ydl: [{
                "start_time": fragment_start,
                "end_time": fragment_end,
            }]
            # Без этого yt-dlp режет по ближайшим ключевым кадрам и границы
            # расходятся на несколько секунд. Повторное кодирование делает
            # обрезку точной.
            opts["force_keyframes_at_cuts"] = True
            # Фрагмент всегда качает ffmpeg. Иногда YouTube отвечает 400 на
            # запрос видеодиапазона, и ffmpeg молча заканчивает файл без
            # видео. Просим ffmpeg повторить такой запрос, а не сдаваться.
            opts["external_downloader_args"] = {
                "ffmpeg_i": [
                    "-reconnect", "1",
                    "-reconnect_streamed", "1",
                    "-reconnect_on_network_error", "1",
                    "-reconnect_on_http_error", "400,403,404,408,429,5xx",
                    "-reconnect_delay_max", "5",
                    "-reconnect_max_retries", "5",
                ],
            }
            self.logger.info("Fragment: start=%s end=%s", fragment_start, fragment_end)

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

        # Ожидаемые дорожки известны уже по выбранному режиму. Точные данные
        # придут из progress hook, но запасной вариант нужен и тогда, когда
        # yt-dlp пропускает уже существующий файл и hook не срабатывает.
        if mode == "audio" or custom_audio_only:
            self._expect_video, self._expect_audio = False, True
        elif mode == "video":
            self._expect_video, self._expect_audio = True, False
        elif mode == "custom":
            # В таблице форматов может быть и видео без звука, и готовая пара.
            # Поэтому для произвольного формата проверяем только видео.
            self._expect_video, self._expect_audio = True, False
        else:
            self._expect_video, self._expect_audio = True, True

        if fragment_enabled:
            # Прошлая неудачная попытка могла оставить файл без видео. Если его
            # не убрать, yt-dlp решит, что всё уже скачано, и не начнёт заново.
            self._remove_broken_fragment(output_dir, fragment_suffix)

        postprocessors: list[dict] = []
        if mode == "audio":
            opts["format"] = "ba/best"
            codec = audio_format if audio_format in ("mp3", "m4a", "opus", "wav", "flac") else "mp3"
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
            if codec not in ("wav", "flac"):
                pp["preferredquality"] = audio_quality
            postprocessors.append(pp)
        elif mode == "video":
            # Только видео, без звуковой дорожки
            opts["format"] = "bv"
        elif mode == "custom":
            opts["format"] = custom_format or "bv*+ba/b"
        else:
            opts["format"] = "bv*+ba/b"

        # Обложку и теги встраиваем только в предсказуемый контейнер: MP4 для
        # видео и аудиоформаты после конвертации. Для произвольного формата из
        # таблицы контейнер неизвестен, поэтому не рискуем повторной загрузкой.
        embed_video = mode in ("best", "video") or (mode == "custom" and "+" in (custom_format or ""))
        if embed_media and (mode == "audio" or embed_video):
            # Сначала приводим обложку к JPEG (иначе WebP не встроить),
            # затем вкладываем её в файл и только потом пишем теги.
            opts["writethumbnail"] = True
            postprocessors.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg"})
            postprocessors.append({"key": "EmbedThumbnail"})

        subtitle_langs = parse_subtitle_langs(sub_langs) if subtitles else []
        if subtitles and not subtitle_langs:
            subtitle_langs = parse_subtitle_langs(SUBTITLE_LANGS_DEFAULT)
        if subtitle_langs:
            opts["writesubtitles"] = True
            opts["subtitleslangs"] = subtitle_langs
            if sub_auto:
                opts["writeautomaticsub"] = True
            # Сначала приводим субтитры к srt: так они читаются плеерами и
            # корректно вшиваются в mp4. Конвертация идёт до скачивания
            # дорожек, встраивание — уже после них.
            postprocessors.append(
                {"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"}
            )
            if sub_embed:
                postprocessors.append({"key": "FFmpegEmbedSubtitle"})

        sponsor_mode = sponsor_mode if sponsor_mode in SPONSOR_MODES else "off"
        categories = normalize_sponsor_categories(sponsor_categories)
        sponsor_enabled = sponsor_mode in ("mark", "remove") and bool(categories)
        if sponsor_enabled:
            postprocessors.append(
                {"key": "SponsorBlock", "categories": categories, "when": "after_filter"}
            )
            # ModifyChapters вырезает сегменты (режим «вырезать») или добавляет
            # их как главы (режим «пометить»). Он должен идти после встраивания
            # субтитров и до FFmpegMetadata.
            postprocessors.append({
                "key": "ModifyChapters",
                "remove_sponsor_segments": categories if sponsor_mode == "remove" else [],
            })

        # Теги нужны всегда для аудио, при встраивании обложки и для глав
        # SponsorBlock. FFmpegMetadata идёт последним.
        if mode == "audio" or (embed_media and embed_video) or sponsor_enabled:
            postprocessors.append({"key": "FFmpegMetadata"})

        if postprocessors:
            opts["postprocessors"] = postprocessors

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

        def run(run_opts: Dict[str, Any]) -> None:
            with YoutubeDL(run_opts) as ydl:
                self._ydl = ydl
                result = ydl.download([url])
            if result:
                raise RuntimeError("Не все файлы удалось скачать. Проверьте доступность видео в плейлисте.")

        def without_thumbnail(run_opts: Dict[str, Any]) -> Dict[str, Any]:
            plain = dict(run_opts)
            plain.pop("writethumbnail", None)
            plain["postprocessors"] = [
                pp for pp in plain.get("postprocessors", [])
                if pp.get("key") not in ("EmbedThumbnail", "FFmpegThumbnailsConvertor")
            ]
            return plain

        def without_subtitle_embedding(run_opts: Dict[str, Any]) -> Dict[str, Any]:
            plain = dict(run_opts)
            plain["postprocessors"] = [
                pp for pp in plain.get("postprocessors", [])
                if pp.get("key") != "FFmpegEmbedSubtitle"
            ]
            return plain

        def without_sponsorblock(run_opts: Dict[str, Any]) -> Dict[str, Any]:
            plain = dict(run_opts)
            plain["postprocessors"] = [
                pp for pp in plain.get("postprocessors", [])
                if pp.get("key") not in ("SponsorBlock", "ModifyChapters")
            ]
            return plain

        def fallback_for(run_opts: Dict[str, Any], error: str):
            """Подбирает вариант без необязательного шага, который сломался."""
            keys = {pp.get("key") for pp in run_opts.get("postprocessors", [])}
            if "SponsorBlock" in keys and self._sponsor_failed(error):
                return without_sponsorblock(run_opts), "SponsorBlock недоступен, продолжаю без него..."
            if "FFmpegEmbedSubtitle" in keys and self._subtitle_embed_failed(error):
                return without_subtitle_embedding(run_opts), "Не удалось вшить субтитры, сохраняю их отдельными файлами..."
            if "EmbedThumbnail" in keys and self._embedding_failed(error):
                return without_thumbnail(run_opts), "Не удалось встроить обложку, сохраняю файл без неё..."
            return None, ""

        try:
            clients_order = client_order(cookies_file, cookies_from_browser)
            attempts = len(clients_order)
            for attempt in range(1, attempts + 1):
                if self._stop_event.is_set():
                    self.progress.status = "cancelled"
                    return

                attempt_opts = dict(opts)
                self.logger.info("Download attempt=%s/%s clients=%s", attempt, attempts, clients_order[attempt - 1])
                ensure_pot_server()
                attempt_opts.update(build_runtime_opts(clients=clients_order[attempt - 1], proxy=proxy))

                # Если необязательный шаг не удался (обложка, вшивание
                # субтитров, SponsorBlock), повторяем без него: сам файл
                # важнее оформления.
                run_opts = attempt_opts
                attempt_error = ""
                fragment_try = 0
                for _ in range(4):
                    try:
                        run(run_opts)
                        if fragment_enabled and not self.progress.is_playlist:
                            problem = self._verify_fragment_output(fragment_suffix, output_dir)
                            if problem:
                                self._discard_broken_output()
                                if fragment_try < FRAGMENT_RETRIES:
                                    fragment_try += 1
                                    attempt_error = problem
                                    self.logger.warning(
                                        "Fragment is incomplete, retrying with fresh links: %s", problem
                                    )
                                    self.progress.retry_message = "Видео не попало в файл, повторяю загрузку..."
                                    self._emit()
                                    run_opts = self._fragment_retry_opts(run_opts)
                                    continue
                                raise DownloadError(problem)
                        attempt_error = ""
                        break
                    except (DownloadError, ExtractorError) as e:
                        if "cancel" in str(e).lower() or self._stop_event.is_set():
                            self.progress.status = "cancelled"
                            return
                        attempt_error = self._clean_error(str(e)) or "Ошибка загрузки"
                        self.logger.warning("Download attempt failed", exc_info=True)
                    except Exception as e:
                        if self._stop_event.is_set():
                            self.progress.status = "cancelled"
                            return
                        attempt_error = self._clean_error(str(e)) or "Неизвестная ошибка"
                        self.logger.warning("Download attempt failed unexpectedly", exc_info=True)

                    fallback, message = fallback_for(run_opts, attempt_error)
                    if fallback is None:
                        break
                    self.logger.info("Optional postprocessing failed, retrying without it: %s", message)
                    self.progress.retry_message = message
                    self._emit()
                    run_opts = fallback

                if not attempt_error:
                    if self._stop_event.is_set():
                        self.progress.status = "cancelled"
                    else:
                        self.progress.status = "done"
                        self.progress.percent = 100.0
                        self.progress.retry_message = ""
                        self.progress.error = ""
                    return

                last_error = attempt_error
                if attempt < attempts and self._should_retry(last_error):
                    self.progress.status = "extracting"
                    self.progress.retry_message = (
                        f"Попытка {attempt} не удалась, пробую способ {attempt + 1} из {attempts}..."
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
    def _embedding_failed(error: str) -> bool:
        low = error.lower()
        return any(marker in low for marker in
                   ("thumbnail", "cover", "mutagen", "attached_pic", "ffprobe", "embed using"))

    @staticmethod
    def _subtitle_embed_failed(error: str) -> bool:
        low = error.lower()
        return any(marker in low for marker in ("subtitle", "субтит", ".srt", ".ass", ".vtt"))

    @staticmethod
    def _sponsor_failed(error: str) -> bool:
        return "sponsorblock" in error.lower()

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
            self._remember_expected_streams(info)
        elif status == "error":
            self.progress.status = "error"
            self.progress.error = str(d.get("error", "Unknown error"))

        self._emit()

    def _remember_expected_streams(self, info: Dict[str, Any]) -> None:
        """Запоминает, какие дорожки должны оказаться в готовом файле.

        YouTube иногда отвечает ошибкой на запрос видеодиапазона, а ffmpeg
        завершает работу без видео и с кодом 0. yt-dlp считает такую загрузку
        успешной. Ожидаемые дорожки берём из выбранных форматов, чтобы потом
        проверить результат.
        """
        requested = info.get("requested_formats") or []
        if requested:
            self._expect_video = any(f.get("vcodec") not in (None, "none") for f in requested)
            self._expect_audio = any(f.get("acodec") not in (None, "none") for f in requested)
        else:
            self._expect_video = info.get("vcodec") not in (None, "none")
            self._expect_audio = info.get("acodec") not in (None, "none")

    @staticmethod
    def _media_streams(path: Path) -> set[str] | None:
        """Типы дорожек в файле или ``None``, если проверить не удалось.

        Обложка тоже является видеопотоком (mjpeg с attached_pic), поэтому её
        не считаем видео.
        """
        ffprobe = find_ffprobe()
        if not ffprobe:
            return None
        try:
            result = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "stream=codec_type:stream_disposition=attached_pic",
                    "-of", "json", str(path),
                ],
                capture_output=True, text=True, timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return None
        kinds: set[str] = set()
        for stream in data.get("streams") or []:
            if stream.get("codec_type") == "video":
                if (stream.get("disposition") or {}).get("attached_pic"):
                    continue
                kinds.add("video")
            elif stream.get("codec_type") == "audio":
                kinds.add("audio")
        return kinds

    def _verify_fragment_output(self, suffix: str = "", output_dir: Path | None = None) -> str:
        """Проверяет, что после обрезки остались нужные дорожки."""
        path = Path(self.progress.filename or "")
        if not path.is_file() and suffix and output_dir is not None:
            path = self._find_fragment_output(suffix, output_dir, self._id_hint) or path
        if not path.is_file():
            return ""
        self.progress.filename = str(path)
        kinds = self._media_streams(path)
        if kinds is None:
            return ""
        if self._expect_video and "video" not in kinds:
            return "В скачанном файле нет видеодорожки — пробую другой способ."
        if self._expect_audio and "audio" not in kinds:
            return "В скачанном файле нет звуковой дорожки — пробую другой способ."
        return ""

    def _remove_broken_fragment(self, output_dir: Path, suffix: str) -> None:
        """Убирает прошлый неполный файл фрагмента до начала загрузки."""
        path = self._find_fragment_output(suffix, output_dir, self._id_hint)
        if path is None:
            return
        kinds = self._media_streams(path)
        if kinds is None:
            return
        if (self._expect_video and "video" not in kinds) or (
            self._expect_audio and "audio" not in kinds
        ):
            self.logger.info("Removing incomplete fragment output: %s", path)
            self._unlink_output(path)

    @staticmethod
    def _find_fragment_output(suffix: str, output_dir: Path, id_hint: str = "") -> Path | None:
        """Ищет готовый файл фрагмента, если yt-dlp не сообщил его имя.

        Так бывает, когда файл уже существует: yt-dlp пишет «already been
        downloaded» и не вызывает progress hook. Идентификатор ролика не даёт
        перепутать файлы с одинаковым суффиксом фрагмента. Без идентификатора
        поиск не выполняется: удалять или проверять чужой файл нельзя.
        """
        if not id_hint:
            return None
        needle = f"[{id_hint}]{suffix}"
        skip = {".part", ".ytdl", ".jpg", ".webp", ".png", ".srt", ".vtt", ".ass", ".json"}
        found: list[Path] = []
        try:
            for path in output_dir.iterdir():
                if needle in path.name and path.is_file() and path.suffix.lower() not in skip:
                    found.append(path)
        except OSError:
            return None
        return max(found, key=lambda p: p.stat().st_mtime, default=None)

    @staticmethod
    def _fragment_retry_opts(run_opts: Dict[str, Any]) -> Dict[str, Any]:
        """На повторе просит H.264: с ним реже срывается запрос диапазона."""
        retry = dict(run_opts)
        if retry.get("format") == "bv*+ba/b":
            retry["format"] = "bv*[vcodec^=avc1]+ba/bv*[vcodec^=avc1]+b/bv*+ba/b"
        return retry

    def _discard_broken_output(self) -> None:
        """Удаляет неполный файл, чтобы следующая попытка не считала его готовым."""
        path = Path(self.progress.filename or "")
        if not path.name:
            return
        self._unlink_output(path)
        self.progress.filename = ""

    def _unlink_output(self, path: Path) -> None:
        for candidate in (path, path.with_name(path.name + ".part"), path.with_name(path.name + ".ytdl")):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                self.logger.warning("Could not remove broken output: %s", candidate, exc_info=True)
