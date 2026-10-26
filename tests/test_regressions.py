import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import downloader
import pot
import utils


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
            response = MagicMock()
            response.__enter__.return_value = response
            response.iter_content.side_effect = OSError("connection lost")
            with patch.object(utils.requests, "get", return_value=response):
                self.assertFalse(utils.download_file("https://example.com/update", dest))
            self.assertEqual(dest.read_bytes(), b"previous")
            self.assertFalse(dest.with_suffix(".exe.part").exists())

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
