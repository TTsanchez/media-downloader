# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import selftest


class ReportTests(unittest.TestCase):
    def test_successful_report_lists_checks(self):
        text = selftest.format_report([
            (selftest.INFO, "Режим", "исходники"),
            (selftest.OK, "Tkinter", "Tk 8.6"),
        ])
        self.assertIn("[OK] Tkinter: Tk 8.6", text)
        self.assertIn("всё в порядке", text)

    def test_failed_check_is_counted(self):
        text = selftest.format_report([
            (selftest.ERROR, "Tkinter", "TclError: no display"),
            (selftest.OK, "Версия приложения", "1.2.0"),
        ])
        self.assertIn("[ОШИБКА] Tkinter: TclError: no display", text)
        self.assertIn("ошибок — 1", text)

    def test_run_returns_failure_for_any_error(self):
        with patch.object(selftest, "_run_checks",
                          return_value=[(selftest.ERROR, "Модули", "ImportError: yt_dlp")]):
            ok, text = selftest.run(write_file=False)
        self.assertFalse(ok)
        self.assertIn("ImportError", text)

    def test_run_writes_report_file(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / "selftest.txt"
            with patch.object(selftest, "_run_checks",
                              return_value=[(selftest.OK, "Tkinter", "Tk 8.6")]), patch.object(
                    selftest, "_report_path", return_value=report):
                ok, text = selftest.run()
            self.assertTrue(ok)
            self.assertIn("Отчёт сохранён", text)
            self.assertIn("[OK] Tkinter", report.read_text(encoding="utf-8"))

    def test_component_summary_never_raises(self):
        with patch.object(selftest.components, "ytdlp_ready", return_value=False), patch.object(
                selftest.components, "ffmpeg_ready", return_value=False):
            summary = selftest._component_summary()
        self.assertIn("yt-dlp не установлен", summary)
        self.assertIn("FFmpeg не установлен", summary)


if __name__ == "__main__":
    unittest.main()
