import tempfile
import threading
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import diagnostics
from downloader import DownloadProgress


class UITests(unittest.TestCase):
    """Реальное окно Tk, без сети, загрузок и пользовательского конфига."""

    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.update_idletasks()
            probe.destroy()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")
        cls.temp = tempfile.TemporaryDirectory()
        cls.patches = [
            patch.object(main, "CONFIG_FILE", Path(cls.temp.name) / "config.json"),
            patch.object(main, "LEGACY_CONFIG_FILE", Path(cls.temp.name) / "legacy.json"),
            patch.object(main, "detect_browsers", return_value=[]),
            patch.object(main.YTApp, "_check_updates_async"),
            patch.object(main.YTApp, "_autostart_pot"),
            patch.object(main.YTApp, "_check_components"),
            patch.object(main.YTApp, "_check_ffmpeg"),
        ]
        for context in cls.patches:
            context.start()
        cls.app = main.YTApp()
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.app._closing = True
        cls.app.destroy()
        for context in reversed(cls.patches):
            context.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.app._q_running = False
        self.app.playlist_all_var.set(True)
        self.app.url_var.set("")
        self.app._queue_clear()

    def test_all_playlist_ignores_old_range(self):
        self.app.playlist_start_var.set(8)
        self.app.playlist_end_var.set(2)
        self.assertEqual(self.app._playlist_range(), (1, 0))

    def test_malformed_playlist_range_is_rejected(self):
        self.app.playlist_all_var.set(False)
        self.app.playlist_start_var.set("invalid")
        with self.assertRaises(ValueError):
            self.app._playlist_range()
        self.app.playlist_start_var.set(1)

    def test_queue_rejects_invalid_and_duplicate_links(self):
        self.assertFalse(self.app._queue_append("not a link"))
        self.assertTrue(self.app._queue_append("https://example.com/video"))
        self.assertFalse(self.app._queue_append("https://example.com/video"))
        self.assertEqual(len(self.app.queue_items), 1)

    def test_late_progress_does_not_overwrite_final_status(self):
        self.app._queue_append("https://example.com/video")
        item = self.app.queue_items[0]
        item.update(status="готово", percent=100)
        progress = DownloadProgress()
        progress.status = "downloading"
        progress.percent = 20
        self.app._queue_on_progress(item, progress)
        self.assertEqual((item["status"], item["percent"]), ("готово", 100))

    def test_changing_url_clears_selected_format(self):
        self.app._selected_format = "137+140"
        self.app.url_var.set("https://example.com/new")
        self.assertIsNone(self.app._selected_format)

    def test_stale_info_is_not_shown_for_another_url(self):
        self.app.url_var.set("https://example.com/new")
        self.app._present_info("https://example.com/old", {"title": "Old title"})
        self.assertNotIn("Old title", self.app.title_lbl.cget("text"))

    def test_worker_can_post_without_calling_tk(self):
        result = []
        thread = threading.Thread(target=lambda: self.app._post_ui(lambda: result.append("done")))
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [])
        self.app._drain_ui_events()
        self.assertEqual(result, ["done"])

    def test_controls_fit_small_window(self):
        self.app.deiconify()
        self.app.minsize(900, 620)
        self.app.geometry("900x700")
        self.app.update()
        for widget in (self.app.btn_download, self.app.btn_stop, self.app.progress_bar, self.app.btn_pause):
            bottom = widget.winfo_rooty() + widget.winfo_height()
            self.assertLessEqual(bottom, self.app.winfo_rooty() + self.app.winfo_height())
        self.assertGreater(self.app.nb.winfo_height(), 100)
        self.app.withdraw()

    def test_preset_selects_a_height_limited_format(self):
        info = {"formats": [
            {"vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 100},
            {"vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 60},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 10},
        ]}
        self.app._populate_formats(info)
        self.app.preset_var.set("720p")
        self.app._on_preset_change()
        mode, custom, audio_only = self.app._current_mode()
        self.assertEqual(mode, "custom")
        self.assertEqual(custom, "bv*[height<=720]+ba/b[height<=720]")
        self.assertFalse(audio_only)
        self.assertEqual(self.app._estimate_size(height=720), 70)
        self.assertIn("70", self.app.fmt_lbl.cget("text"))

    def test_estimate_uses_best_video_and_audio(self):
        info = {"formats": [
            {"vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 100},
            {"vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 60},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 10},
        ]}
        self.app.format_var.set("best")
        self.app._selected_format = None
        self.assertEqual(self.app._estimate_size(info), 110)
        self.app.format_var.set("bestaudio")
        self.assertEqual(self.app._estimate_size(info), 10)

    def test_queue_survives_restart_snapshot(self):
        self.assertTrue(self.app._queue_append("https://example.com/kept"))
        self.app.queue_items[0]["fmt_label"] = "1080p"
        snapshot = self.app._queue_snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["fmt_label"], "1080p")
        self.app.queue_items[0]["status"] = "готово"
        self.assertEqual(self.app._queue_snapshot(), [])
        self.app.queue_items[0]["status"] = "пауза"
        self.app._queue_clear()
        self.app.config[main.QUEUE_CONFIG_KEY] = snapshot
        self.app._restore_queue()
        self.assertEqual(len(self.app.queue_items), 1)
        self.assertEqual(self.app.queue_items[0]["url"], "https://example.com/kept")
        self.assertEqual(self.app.queue_items[0]["status"], "ожидание")

    def test_queue_accepts_links_from_a_text_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "links.txt"
            path.write_text("# comment\nhttps://example.com/one\nhttps://example.com/two\n", encoding="utf-8")
            self.app._queue_add_paths([path])
        self.assertEqual([it["url"] for it in self.app.queue_items],
                         ["https://example.com/one", "https://example.com/two"])

    def test_queue_pause_marks_unfinished_items(self):
        self.assertTrue(self.app._queue_append("https://example.com/pause"))
        self.app._q_running = True
        self.app._queue_pause()
        try:
            self.assertTrue(self.app._q_paused)
            self.assertEqual(self.app.queue_items[0]["status"], "пауза")
            self.app._queue_finish()
            self.assertEqual(str(self.app.btn_q_start.cget("text")), "Продолжить")
        finally:
            self.app._q_running = False
            self.app._q_paused = False

    def test_single_download_pause_offers_resume(self):
        self.app.url_var.set("https://example.com/video")
        self.app.is_downloading = True
        self.app._last_download_args = {"url": "https://example.com/video"}
        with patch.object(self.app.downloader, "stop") as stop:
            self.app._pause_download()
        stop.assert_called_once()
        try:
            self.assertTrue(self.app._paused_download)
            self.app._finish()
            self.assertEqual(str(self.app.btn_download.cget("text")), "Продолжить")
            self.assertFalse(self.app.is_downloading)
        finally:
            self.app._paused_download = False
            self.app._last_download_args = None
            self.app.btn_download.configure(text="Скачать")

    def test_callback_error_is_logged_and_reported_inside_app(self):
        with tempfile.TemporaryDirectory() as folder:
            service = diagnostics.Diagnostics(folder, install_hooks=False, enable_faults=False)
            try:
                with patch.object(diagnostics, "_service", service), patch.object(main.messagebox, "showerror") as show:
                    def fail():
                        raise RuntimeError("test-ui-callback-failure")
                    self.app._last_error_notice = 0
                    self.app._post_ui(fail)
                    self.app._drain_ui_events()
                    show.assert_called_once()
                    self.assertIn("Обнаружена ошибка", self.app.status_var.get())
                text = service.log_file.read_text(encoding="utf-8")
                self.assertIn("Traceback", text)
                self.assertIn("test-ui-callback-failure", text)
            finally:
                service.close()

    def test_export_button_creates_support_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            service = diagnostics.Diagnostics(Path(folder) / "logs", install_hooks=False, enable_faults=False)
            workers = []
            actual_thread = threading.Thread

            def worker(*args, **kwargs):
                result = actual_thread(*args, **kwargs)
                workers.append(result)
                return result

            try:
                destination = Path(folder) / "support.zip"
                with patch.object(main, "get_diagnostics", return_value=service), patch.object(
                    main.filedialog, "asksaveasfilename", return_value=str(destination)
                ), patch.object(main.messagebox, "showinfo") as show, patch.object(main.threading, "Thread", side_effect=worker):
                    self.app._export_report()
                    self.assertTrue(self.app._exporting_report)
                    for task in workers:
                        task.join(timeout=2)
                        self.assertFalse(task.is_alive())
                    self.app._drain_ui_events()
                    self.assertTrue(destination.is_file())
                    self.assertFalse(self.app._exporting_report)
                    show.assert_called_once()
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
