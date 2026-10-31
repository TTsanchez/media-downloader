import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import downloader
import net
import pot
import utils


class UrlCleaningTests(unittest.TestCase):
    def test_mix_radio_is_reduced_to_single_video(self):
        url = "https://www.youtube.com/watch?v=abc123&list=RDabc123&index=2&start_radio=1"
        self.assertEqual(utils.clean_media_url(url), "https://www.youtube.com/watch?v=abc123")

    def test_explicit_playlist_is_kept(self):
        cleaned = utils.clean_media_url("https://www.youtube.com/watch?v=abc123&list=PLxyz&index=4")
        self.assertIn("list=PLxyz", cleaned)
        self.assertNotIn("index=", cleaned)

    def test_playlist_url_is_untouched(self):
        url = "https://www.youtube.com/playlist?list=PLxyz"
        self.assertEqual(utils.clean_media_url(url), url)

    def test_short_link_mix_is_cleaned(self):
        self.assertEqual(
            utils.clean_media_url("https://youtu.be/abc123?list=RDabc123"),
            "https://youtu.be/abc123",
        )

    def test_trailing_punctuation_and_tiktok_tracking(self):
        self.assertEqual(utils.clean_media_url("https://youtu.be/abc123)."), "https://youtu.be/abc123")
        self.assertEqual(
            utils.clean_media_url("https://www.tiktok.com/@a/video/123?is_from_webapp=1"),
            "https://www.tiktok.com/@a/video/123",
        )

    def test_music_domains(self):
        for url in (
            "https://music.youtube.com/watch?v=abc123",
            "https://soundcloud.com/artist/track",
            "https://artist.bandcamp.com/track/song",
            "https://www.mixcloud.com/artist/show",
            "https://audiomack.com/artist/song",
        ):
            self.assertTrue(utils.is_music_url(url), url)
        self.assertFalse(utils.is_music_url("https://www.youtube.com/watch?v=abc123"))
        self.assertFalse(utils.is_music_url("https://soundcloud.com.evil.example/track"))



class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.ydl = MagicMock()
        self.ydl.return_value.__enter__.return_value.download.return_value = 0
        for target, value in (
            ("YoutubeDL", self.ydl), ("ensure_pot_server", MagicMock()),
            ("build_runtime_opts", MagicMock(return_value={})),
            ("find_ffmpeg", MagicMock(return_value=None)),
        ):
            context = patch.object(downloader, target, value)
            context.start()
            self.addCleanup(context.stop)

    def test_full_title_when_name_limit_disabled(self):
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/video", self.output, restrict_filenames=False)
        template = self.ydl.call_args.args[0]["outtmpl"]
        self.assertIn("%(title)s", template)
        self.assertNotIn(".0s", template)
        self.assertEqual(dl.progress.status, "done")

    def test_video_only_does_not_select_muxed_formats(self):
        downloader.MediaDownloader().download("https://example.com/video", self.output, mode="video")
        self.assertEqual(self.ydl.call_args.args[0]["format"], "bv")

    def test_whole_playlist_does_not_limit_items(self):
        downloader.MediaDownloader().download("https://example.com/list", self.output)
        self.assertNotIn("playlist_items", self.ydl.call_args.args[0])

    def test_invalid_range_reports_error_without_downloading(self):
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/list", self.output, playlist_start=5, playlist_end=2)
        self.assertEqual(dl.progress.status, "error")
        self.ydl.assert_not_called()

    def test_partial_playlist_failure_is_not_success(self):
        self.ydl.return_value.__enter__.return_value.download.return_value = 1
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/list", self.output, playlist_end=5)
        self.assertEqual(dl.progress.status, "error")
        self.assertIn("Не все", dl.progress.error)

    def test_stop_before_worker_starts_is_respected(self):
        dl = downloader.MediaDownloader()
        dl.stop()
        dl.download("https://example.com/video", self.output)
        self.assertEqual(dl.progress.status, "cancelled")
        self.ydl.assert_not_called()

    def test_progress_events_are_independent_snapshots(self):
        events = []
        dl = downloader.MediaDownloader(events.append)
        dl.progress.percent = 10
        dl._emit()
        dl.progress.percent = 90
        dl._emit()
        self.assertEqual([p.percent for p in events], [10, 90])

    def test_directory_error_is_reported(self):
        file = self.output / "file"
        file.write_text("occupied")
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/video", file)
        self.assertEqual(dl.progress.status, "error")
        self.ydl.assert_not_called()

    def test_empty_format_list_tries_the_next_client(self):
        failure = downloader.DownloadError("ERROR: [youtube] x: Requested format is not available")
        success = MagicMock()
        success.__enter__.return_value.download.return_value = 0
        self.ydl.side_effect = [failure, success]
        dl = downloader.MediaDownloader()
        dl._stop_event.wait = MagicMock(return_value=False)
        dl.download("https://example.com/video", self.output)
        self.assertEqual(self.ydl.call_count, 2)
        self.assertEqual(dl.progress.status, "done")

    def test_download_does_not_chunk_http_requests(self):
        downloader.MediaDownloader().download("https://example.com/video", self.output)
        opts = self.ydl.call_args.args[0]
        self.assertNotIn("http_chunk_size", opts)
        self.assertEqual(opts["concurrent_fragment_downloads"], 8)

    def test_anonymous_clients_are_tried_before_web(self):
        self.assertIn(["visionos"], downloader.CLIENT_FALLBACKS)
        self.assertLess(
            downloader.CLIENT_FALLBACKS.index(["visionos"]),
            downloader.CLIENT_FALLBACKS.index(["web"]),
        )

    def test_mix_link_is_cleaned_before_download(self):
        dl = downloader.MediaDownloader()
        dl.download(
            "https://www.youtube.com/watch?v=abc123&list=RDabc123&start_radio=1",
            self.output,
        )
        self.assertEqual(dl._url, "https://www.youtube.com/watch?v=abc123")

    def test_audio_flac_uses_lossless_codec(self):
        downloader.MediaDownloader().download(
            "https://example.com/video", self.output, mode="audio", audio_format="flac"
        )
        postprocessors = self.ydl.call_args.args[0]["postprocessors"]
        extract = next(pp for pp in postprocessors if pp["key"] == "FFmpegExtractAudio")
        self.assertEqual(extract["preferredcodec"], "flac")
        self.assertNotIn("preferredquality", extract)

    def test_audio_embeds_cover_and_tags(self):
        downloader.MediaDownloader().download(
            "https://example.com/video", self.output, mode="audio", audio_format="mp3"
        )
        opts = self.ydl.call_args.args[0]
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertTrue(opts["writethumbnail"])
        self.assertIn("FFmpegThumbnailsConvertor", keys)
        self.assertIn("EmbedThumbnail", keys)
        self.assertIn("FFmpegMetadata", keys)

    def test_video_embeds_cover_and_tags(self):
        downloader.MediaDownloader().download("https://example.com/video", self.output)
        opts = self.ydl.call_args.args[0]
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertTrue(opts["writethumbnail"])
        self.assertIn("EmbedThumbnail", keys)
        self.assertIn("FFmpegMetadata", keys)

    def test_embedding_can_be_disabled(self):
        downloader.MediaDownloader().download(
            "https://example.com/video", self.output, embed_media=False
        )
        opts = self.ydl.call_args.args[0]
        self.assertNotIn("writethumbnail", opts)
        self.assertNotIn("postprocessors", opts)

    def test_thumbnail_failure_retries_without_embedding(self):
        failure = downloader.DownloadError("ERROR: Postprocessing: Error embedding thumbnail")
        success = MagicMock()
        success.__enter__.return_value.download.return_value = 0
        self.ydl.side_effect = [failure, success]
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/video", self.output)
        self.assertEqual(self.ydl.call_count, 2)
        first, second = self.ydl.call_args_list[0].args[0], self.ydl.call_args_list[1].args[0]
        self.assertTrue(first["writethumbnail"])
        self.assertNotIn("writethumbnail", second)
        self.assertEqual(dl.progress.status, "done")

    def test_subtitles_use_requested_languages_and_embed_into_video(self):
        downloader.MediaDownloader().download(
            "https://example.com/video", self.output, subtitles=True, sub_langs="ru,en"
        )
        opts = self.ydl.call_args.args[0]
        self.assertTrue(opts["writesubtitles"])
        self.assertEqual(opts["subtitleslangs"], ["ru", "en"])
        self.assertNotIn("writeautomaticsub", opts)
        postprocessors = opts["postprocessors"]
        convertor = next(pp for pp in postprocessors if pp["key"] == "FFmpegSubtitlesConvertor")
        self.assertEqual(convertor["format"], "srt")
        self.assertEqual(convertor["when"], "before_dl")
        self.assertIn("FFmpegEmbedSubtitle", [pp["key"] for pp in postprocessors])

    def test_auto_subtitles_can_be_kept_as_separate_files(self):
        downloader.MediaDownloader().download(
            "https://example.com/video",
            self.output,
            subtitles=True,
            sub_langs="ru, en, ru",
            sub_auto=True,
            sub_embed=False,
        )
        opts = self.ydl.call_args.args[0]
        self.assertTrue(opts["writeautomaticsub"])
        self.assertEqual(opts["subtitleslangs"], ["ru", "en"])
        keys = [pp["key"] for pp in opts["postprocessors"]]
        self.assertIn("FFmpegSubtitlesConvertor", keys)
        self.assertNotIn("FFmpegEmbedSubtitle", keys)

    def test_subtitle_languages_drop_invalid_tokens(self):
        self.assertEqual(
            downloader.parse_subtitle_langs(" en, ru;, en-US, b@d code! "),
            ["en", "ru", "en-US"],
        )
        downloader.MediaDownloader().download(
            "https://example.com/video", self.output, subtitles=True, sub_langs="??"
        )
        opts = self.ydl.call_args.args[0]
        self.assertEqual(opts["subtitleslangs"], ["ru", "en"])

    def test_sponsorblock_marks_default_categories(self):
        downloader.MediaDownloader().download(
            "https://example.com/video", self.output, sponsor_mode="mark"
        )
        opts = self.ydl.call_args.args[0]
        postprocessors = opts["postprocessors"]
        sponsor = next(pp for pp in postprocessors if pp["key"] == "SponsorBlock")
        self.assertEqual(sponsor["categories"], list(downloader.SPONSOR_DEFAULT_CATEGORIES))
        self.assertEqual(sponsor["when"], "after_filter")
        modify = next(pp for pp in postprocessors if pp["key"] == "ModifyChapters")
        self.assertEqual(modify["remove_sponsor_segments"], [])
        self.assertIn("FFmpegMetadata", [pp["key"] for pp in postprocessors])

    def test_sponsorblock_removes_only_selected_categories(self):
        downloader.MediaDownloader().download(
            "https://example.com/video",
            self.output,
            sponsor_mode="remove",
            sponsor_categories=["intro", "sponsor", "unknown"],
        )
        opts = self.ydl.call_args.args[0]
        postprocessors = opts["postprocessors"]
        sponsor = next(pp for pp in postprocessors if pp["key"] == "SponsorBlock")
        self.assertEqual(sponsor["categories"], ["sponsor", "intro"])
        modify = next(pp for pp in postprocessors if pp["key"] == "ModifyChapters")
        self.assertEqual(modify["remove_sponsor_segments"], ["sponsor", "intro"])

    def test_sponsorblock_is_off_by_default_and_without_categories(self):
        downloader.MediaDownloader().download("https://example.com/video", self.output)
        keys = [pp["key"] for pp in self.ydl.call_args.args[0].get("postprocessors", [])]
        self.assertNotIn("SponsorBlock", keys)
        self.ydl.reset_mock()
        downloader.MediaDownloader().download(
            "https://example.com/video",
            self.output,
            sponsor_mode="mark",
            sponsor_categories=[],
        )
        keys = [pp["key"] for pp in self.ydl.call_args.args[0].get("postprocessors", [])]
        self.assertNotIn("SponsorBlock", keys)

    def test_sponsorblock_failure_retries_without_it(self):
        failure = downloader.DownloadError(
            "ERROR: Postprocessing: Unable to communicate with SponsorBlock API: timed out"
        )
        success = MagicMock()
        success.__enter__.return_value.download.return_value = 0
        self.ydl.side_effect = [failure, success]
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/video", self.output, sponsor_mode="mark")
        self.assertEqual(self.ydl.call_count, 2)
        first = [pp["key"] for pp in self.ydl.call_args_list[0].args[0]["postprocessors"]]
        second = [pp["key"] for pp in self.ydl.call_args_list[1].args[0]["postprocessors"]]
        self.assertIn("SponsorBlock", first)
        self.assertNotIn("SponsorBlock", second)
        self.assertNotIn("ModifyChapters", second)
        self.assertEqual(dl.progress.status, "done")

    def test_subtitle_embedding_failure_keeps_separate_subtitles(self):
        failure = downloader.DownloadError(
            "ERROR: Postprocessing: Unable to embed subtitles into video"
        )
        success = MagicMock()
        success.__enter__.return_value.download.return_value = 0
        self.ydl.side_effect = [failure, success]
        dl = downloader.MediaDownloader()
        dl.download("https://example.com/video", self.output, subtitles=True)
        self.assertEqual(self.ydl.call_count, 2)
        second = self.ydl.call_args_list[1].args[0]
        self.assertTrue(second["writesubtitles"])
        keys = [pp["key"] for pp in second["postprocessors"]]
        self.assertIn("FFmpegSubtitlesConvertor", keys)
        self.assertNotIn("FFmpegEmbedSubtitle", keys)
        self.assertEqual(dl.progress.status, "done")


class UpdateTests(unittest.TestCase):
    def test_frozen_version_is_loaded_from_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "version.txt").write_text("1.2.3", encoding="utf-8")
            with patch.object(utils.sys, "frozen", True, create=True), patch.object(
                utils.sys, "_MEIPASS", folder, create=True
            ):
                self.assertEqual(utils.get_app_version(), "1.2.3")

    def test_failed_download_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "update.exe"
            dest.write_bytes(b"previous")
            with patch.object(net, "download", side_effect=OSError("connection lost")):
                self.assertFalse(utils.download_file("https://example.com/update", dest))
            self.assertEqual(dest.read_bytes(), b"previous")
            self.assertFalse(dest.with_suffix(".exe.part").exists())

    def test_update_asset_exposes_digest(self):
        release = {"assets": [{
            "name": "Media-Downloader.exe",
            "browser_download_url": "https://example.com/app.exe",
            "digest": "sha256:abc123",
        }]}
        asset = utils.get_app_asset(release)
        self.assertEqual(asset["digest"], "sha256:abc123")
        self.assertEqual(utils.get_app_asset_url(release), "https://example.com/app.exe")

    def test_updater_preserves_cyrillic_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            current = Path(folder) / "Загрузчик.exe"
            diagnostic = SimpleNamespace(directory=Path(folder) / "logs", session_id="test")
            with patch.object(utils, "get_diagnostics", return_value=diagnostic):
                script = utils.create_updater_script(current.with_suffix(".new.exe"), current)
            self.assertIsNotNone(script)
            self.assertIn(str(current), script.read_text(encoding="utf-8"))


class ProviderTests(unittest.TestCase):
    def test_shutdown_does_not_kill_external_server(self):
        with patch.object(pot, "_proc", None), patch.object(pot.subprocess, "run") as run:
            pot.stop_server()
            run.assert_not_called()

    def test_startup_is_serialized(self):
        active = 0
        maximum = 0
        lock = threading.Lock()

        def start(log):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            threading.Event().wait(0.02)
            with lock:
                active -= 1
            return True

        with patch.object(pot, "_start_server", side_effect=start):
            workers = [threading.Thread(target=pot.start_server) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
