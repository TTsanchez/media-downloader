import tempfile
import threading
import tkinter as tk
import unittest
import json
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
        cls.history_file = Path(cls.temp.name) / "history.json"
        cls.patches = [
            patch.object(main, "CONFIG_FILE", Path(cls.temp.name) / "config.json"),
            patch.object(main, "LEGACY_CONFIG_FILE", Path(cls.temp.name) / "legacy.json"),
            patch.object(main, "HISTORY_FILE", cls.history_file),
            patch.object(main, "detect_browsers", return_value=[]),
            patch.object(main.YTApp, "_check_updates_async"),
            patch.object(main.YTApp, "_autostart_pot"),
            patch.object(main.YTApp, "_check_components"),
            patch.object(main.YTApp, "_check_ffmpeg"),
            patch.object(main.thumbnails, "prune", return_value=0),
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
        self.app._q_done_count = 0
        self.app._q_error_count = 0
        self.app._q_cancel_count = 0
        self.app._q_paused = False
        self.app.playlist_all_var.set(True)
        self.app.url_var.set("")
        self.app.subtitles_var.set(False)
        self.app.sub_langs_var.set(main.SUBTITLE_LANGS_DEFAULT)
        self.app.sub_auto_var.set(False)
        self.app.sub_embed_var.set(True)
        self.app.sponsor_mode_var.set(main.SPONSOR_MODE_LABELS["off"])
        for category, variable in self.app.sponsor_cat_vars.items():
            variable.set(category in main.SPONSOR_DEFAULT_CATEGORIES)
        self.app.history.clear()
        self.app._history_dirty = True
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

    def test_queue_cleans_mix_link_and_switches_to_audio(self):
        self.app.format_var.set("best")
        self.app.auto_audio_var.set(True)
        self.assertTrue(
            self.app._queue_append(
                "https://music.youtube.com/watch?v=abc123&list=RDAMVMabc123&start_radio=1"
            )
        )
        item = self.app.queue_items[-1]
        self.assertEqual(item["url"], "https://music.youtube.com/watch?v=abc123")
        self.assertEqual(item["mode"], "audio")
        self.assertEqual(item["fmt_label"], "Аудио")

    def test_queue_keeps_explicit_playlist_links(self):
        self.app.format_var.set("best")
        self.app.auto_audio_var.set(True)
        self.assertTrue(
            self.app._queue_append("https://www.youtube.com/playlist?list=PLxyz")
        )
        self.assertEqual(self.app.queue_items[-1]["mode"], "best")

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

    def test_action_bar_follows_active_tab(self):
        self.app.nb.select(self.app._queue_tab)
        self.app.update()
        self.assertEqual(self.app.action_queue.winfo_manager(), "pack")
        self.assertEqual(self.app.action_main.winfo_manager(), "")
        self.app.nb.select(0)
        self.app.update()
        self.assertEqual(self.app.action_main.winfo_manager(), "pack")
        self.assertEqual(self.app.action_queue.winfo_manager(), "")

    def test_log_is_hidden_by_default_and_toggles(self):
        if self.app._log_visible:
            self.app._toggle_log()
        self.assertFalse(bool(self.app.log_box.grid_info()))
        self.assertEqual(str(self.app.btn_log.cget("text")), "Журнал")
        self.app._toggle_log()
        try:
            self.assertTrue(bool(self.app.log_box.grid_info()))
            self.assertEqual(str(self.app.btn_log.cget("text")), "Скрыть")
        finally:
            self.app._toggle_log()
        self.assertFalse(bool(self.app.log_box.grid_info()))

    def test_info_panel_has_no_cover_hints(self):
        self.assertFalse(hasattr(self.app, "preview_hint"))
        self.app._reset_info_panel()
        self.assertEqual(str(self.app.title_lbl.cget("text")), "Ссылка не выбрана")
        self.assertEqual(str(self.app.meta_lbl.cget("text")), "")

    def test_queue_actions_are_visible_without_scrolling(self):
        self.app.deiconify()
        self.app.geometry("1000x880")
        self.app.nb.select(self.app._queue_tab)
        self.app.update()
        try:
            window_bottom = self.app.winfo_rooty() + self.app.winfo_height()
            for widget in (self.app.btn_q_start, self.app.btn_q_pause, self.app.btn_q_stop):
                self.assertEqual(widget.winfo_manager(), "pack")
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), window_bottom)
            self.assertGreaterEqual(self.app.queue_tree.winfo_height(), 64 * 3)
        finally:
            self.app.nb.select(0)
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

    def test_queue_keeps_subtitle_and_sponsor_settings(self):
        self.app.subtitles_var.set(True)
        self.app.sub_langs_var.set("ru, en")
        self.app.sub_auto_var.set(True)
        self.app.sub_embed_var.set(False)
        self.app.sponsor_mode_var.set(main.SPONSOR_MODE_LABELS["remove"])
        self.app.sponsor_cat_vars["filler"].set(True)
        self.assertTrue(self.app._queue_append("https://example.com/subtitles"))

        item = self.app.queue_items[0]
        self.assertTrue(item["subtitles"])
        self.assertEqual(item["sub_langs"], "ru, en")
        self.assertTrue(item["sub_auto"])
        self.assertFalse(item["sub_embed"])
        self.assertEqual(item["sponsor_mode"], "remove")
        self.assertIn("filler", item["sponsor_categories"])

        snapshot = self.app._queue_snapshot()
        self.app._queue_clear()
        self.app.config[main.QUEUE_CONFIG_KEY] = snapshot
        self.app._restore_queue()
        restored = self.app.queue_items[0]
        self.assertTrue(restored["subtitles"])
        self.assertEqual(restored["sub_langs"], "ru, en")
        self.assertTrue(restored["sub_auto"])
        self.assertFalse(restored["sub_embed"])
        self.assertEqual(restored["sponsor_mode"], "remove")
        self.assertIn("filler", restored["sponsor_categories"])

    def test_settings_save_and_restore_subtitles_and_sponsorblock(self):
        self.app.subtitles_var.set(True)
        self.app.sub_langs_var.set("ru,en-US")
        self.app.sub_auto_var.set(True)
        self.app.sub_embed_var.set(False)
        self.app.sponsor_mode_var.set(main.SPONSOR_MODE_LABELS["mark"])
        self.app.sponsor_cat_vars["outro"].set(False)
        self.app._save_config()

        data = json.loads(main.CONFIG_FILE.read_text(encoding="utf-8"))
        self.assertTrue(data["subtitles"])
        self.assertEqual(data["sub_langs"], "ru,en-US")
        self.assertTrue(data["sub_auto"])
        self.assertFalse(data["sub_embed"])
        self.assertEqual(data["sponsor_mode"], "mark")
        self.assertNotIn("outro", data["sponsor_categories"])

        self.app.subtitles_var.set(False)
        self.app.sponsor_mode_var.set(main.SPONSOR_MODE_LABELS["off"])
        self.app.sponsor_cat_vars["outro"].set(True)
        self.app.config = data
        self.app._apply_config()
        self.assertTrue(self.app.subtitles_var.get())
        self.assertEqual(self.app.sub_langs_var.get(), "ru,en-US")
        self.assertTrue(self.app.sub_auto_var.get())
        self.assertFalse(self.app.sub_embed_var.get())
        self.assertEqual(self.app.sponsor_mode_var.get(), main.SPONSOR_MODE_LABELS["mark"])
        self.assertFalse(self.app.sponsor_cat_vars["outro"].get())

    def test_queue_accepts_links_from_a_text_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "links.txt"
            path.write_text("# comment\nhttps://example.com/one\nhttps://example.com/two\n", encoding="utf-8")
            self.app._queue_add_paths([path])
        self.assertEqual([it["url"] for it in self.app.queue_items],
                         ["https://example.com/one", "https://example.com/two"])

    def test_queue_import_accepts_a_file_with_utf8_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "links.txt"
            path.write_text("https://example.com/bom\n", encoding="utf-8-sig")
            self.app._queue_add_paths([path])
        self.assertEqual([it["url"] for it in self.app.queue_items], ["https://example.com/bom"])

    def test_queue_import_accepts_a_utf16_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "links.txt"
            path.write_text("https://example.com/utf16\n", encoding="utf-16")
            self.app._queue_add_paths([path])
        self.assertEqual([it["url"] for it in self.app.queue_items], ["https://example.com/utf16"])

    def test_queue_snapshot_skips_finished_items_before_the_limit(self):
        saved = self.app.queue_items
        self.app.queue_items = [
            {"url": f"https://example.com/done{i}", "status": "готово"}
            for i in range(main.MAX_SAVED_QUEUE + 5)
        ]
        self.app.queue_items.extend(
            {"url": f"https://example.com/pending{i}", "status": "ожидание"} for i in range(3)
        )
        try:
            snapshot = self.app._queue_snapshot()
        finally:
            self.app.queue_items = saved
        self.assertEqual(
            [entry["url"] for entry in snapshot],
            [f"https://example.com/pending{i}" for i in range(3)],
        )

    def test_queue_restore_limits_entries(self):
        entries = [{"url": f"https://example.com/{i}"} for i in range(5)]
        try:
            with patch.object(main, "MAX_SAVED_QUEUE", 2):
                self.app.config[main.QUEUE_CONFIG_KEY] = entries
                self.app._restore_queue()
        finally:
            self.app.config[main.QUEUE_CONFIG_KEY] = []
        self.assertEqual(len(self.app.queue_items), 2)

    def test_queue_completion_counts_only_the_current_run(self):
        class FakeDownloader:
            def __init__(self, *args, **kwargs):
                self.progress = DownloadProgress()

            def download(self, **kwargs):
                self.progress.status = "done"

            def stop(self):
                pass

        self.app._queue_append("https://example.com/new")
        self.app._queue_append("https://example.com/old")
        new_item, old_item = self.app.queue_items
        old_item["status"] = "готово"
        self.app._q_done_count = 0
        with tempfile.TemporaryDirectory() as folder:
            new_item["options"] = {"output_dir": Path(folder)}
            with patch.object(main, "MediaDownloader", FakeDownloader):
                self.app._queue_download_item(new_item)
        self.app._drain_ui_events()
        self.assertEqual(new_item["status"], "готово")
        self.app._q_running = True
        try:
            with patch.object(self.app, "_show_completion") as shown:
                self.app._queue_finish()
        finally:
            self.app._q_running = False
        shown.assert_called_once()
        self.assertIn("Скачано файлов: 1.", shown.call_args[0][1])

    def test_paused_item_ignores_late_progress(self):
        self.app._queue_append("https://example.com/paused")
        item = self.app.queue_items[0]
        item.update(status="пауза", percent=40)
        progress = DownloadProgress()
        progress.status = "downloading"
        progress.percent = 55
        self.app._queue_on_progress(item, progress)
        self.assertEqual((item["status"], item["percent"]), ("пауза", 40))

    def test_download_waits_for_component_installation(self):
        self.app.url_var.set("https://example.com/video")
        self.app._installing_components = True
        try:
            with patch.object(main.messagebox, "showinfo") as info:
                self.app._download_async()
        finally:
            self.app._installing_components = False
        info.assert_called_once()
        self.assertFalse(self.app.is_downloading)

    def test_queue_does_not_start_while_a_download_is_paused(self):
        self.app._paused_download = True
        try:
            with patch.object(main.messagebox, "showinfo") as info:
                self.app._queue_start()
        finally:
            self.app._paused_download = False
        info.assert_called_once()
        self.assertFalse(self.app._q_running)

    def test_single_download_does_not_resume_during_queue(self):
        self.app.url_var.set("https://example.com/video")
        self.app._paused_download = True
        self.app._last_download_args = {"url": "https://example.com/video"}
        self.app._q_running = True
        try:
            with patch.object(main.messagebox, "showinfo") as info:
                self.app._download_async()
        finally:
            self.app._q_running = False
            self.app._paused_download = False
            self.app._last_download_args = None
        info.assert_called_once()
        self.assertFalse(self.app.is_downloading)

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


    def test_queue_rows_keep_preview_column_and_statuses(self):
        self.assertTrue(self.app._queue_append("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(self.app._queue_append("https://example.com/second"))
        first, second = self.app.queue_items
        self.assertIn("dQw4w9WgXcQ", first["thumb_url"])
        self.assertEqual(self.app.queue_tree.set(first["iid"], "title"),
                         "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(self.app.queue_tree.set(first["iid"], "status"), "ожидание")
        self.assertIn("odd", self.app.queue_tree.item(second["iid"], "tags"))
        self.assertNotIn("odd", self.app.queue_tree.item(first["iid"], "tags"))

    def test_finished_queue_item_is_written_to_history(self):
        class FakeDownloader:
            def __init__(self, *args, **kwargs):
                self.progress = DownloadProgress()

            def download(self, **kwargs):
                self.progress.status = "done"
                self.progress.total = 1024

            def stop(self):
                pass

        self.app._queue_append("https://example.com/history")
        item = self.app.queue_items[0]
        with tempfile.TemporaryDirectory() as folder:
            item["options"] = {"output_dir": Path(folder)}
            with patch.object(main, "MediaDownloader", FakeDownloader):
                self.app._queue_download_item(item)
        entries = self.app.history.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], "https://example.com/history")
        self.assertEqual(entries[0]["size"], 1024)
        self.assertEqual(entries[0]["source"], "queue")

    def test_history_tab_renders_cards_and_count(self):
        self.app.history.add({
            "url": "https://example.com/a",
            "title": "Первое видео",
            "folder": str(Path(tempfile.gettempdir())),
            "file": "a.mp4",
        })
        self.app.history.add({
            "url": "https://example.com/b",
            "title": "Второе видео",
            "folder": str(Path(tempfile.gettempdir())),
            "file": "b.mp4",
        })
        self.app._render_history()
        self.assertEqual(self.app.history_count_lbl.cget("text"), "Всего: 2")
        self.assertGreaterEqual(len(self.app.history_list.winfo_children()), 2)

    def test_desktop_notification_only_when_window_is_inactive(self):
        with patch.object(self.app, "_window_is_active", return_value=True), patch.object(
            self.app._notifier, "show"
        ) as show:
            self.app._notify_desktop("Готово", "Файл сохранён")
            show.assert_not_called()
        with patch.object(self.app, "_window_is_active", return_value=False), patch.object(
            self.app._notifier, "show", return_value=True
        ) as show:
            self.app.notify_var.set(True)
            self.app._notify_desktop("Готово", "Файл сохранён")
            show.assert_called_once_with("Готово", "Файл сохранён")

    def test_preview_uses_youtube_thumbnail_guess(self):
        with patch.object(main.thumbnails, "cached_path", return_value=None), patch.object(
            self.app, "_fetch_thumb"
        ) as fetch:
            self.app._show_preview("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(self.app._preview_key, "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg")
        fetch.assert_called_once()

    def test_placeholder_image_is_created(self):
        photo = self.app._placeholder_photo(main.QUEUE_THUMB, radius=8)
        self.assertIsNotNone(photo)


if __name__ == "__main__":
    unittest.main()
