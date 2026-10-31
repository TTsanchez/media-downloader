# -*- coding: utf-8 -*-
import subprocess
import sys
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bootstrap
import components


class EnsureMinimumTests(unittest.TestCase):
    def test_missing_nothing_does_not_install(self):
        with patch.object(bootstrap, "ytdlp_ready", return_value=True), patch.object(
            bootstrap, "embed_ready", return_value=True
        ), patch.object(bootstrap, "ffmpeg_ready", return_value=True), patch.object(
            bootstrap, "install_ytdlp"
        ) as ytdlp, patch.object(bootstrap, "install_ffmpeg") as ffmpeg:
            self.assertEqual(bootstrap.ensure_minimum(), [])
        ytdlp.assert_not_called()
        ffmpeg.assert_not_called()

    def test_missing_cover_support_is_installed_without_blocking(self):
        calls = []
        with patch.object(bootstrap, "ytdlp_ready", return_value=True), patch.object(
            bootstrap, "embed_ready", return_value=False
        ), patch.object(bootstrap, "ffmpeg_ready", return_value=True), patch.object(
            bootstrap, "install_ytdlp",
            side_effect=lambda log=None, progress=None: calls.append("embed"),
        ):
            errors = bootstrap.ensure_minimum()
        self.assertEqual(calls, ["embed"])
        self.assertEqual(errors, [])

    def test_cover_support_failure_is_not_fatal(self):
        def broken(log=None, progress=None):
            raise RuntimeError("offline")

        with patch.object(bootstrap, "ytdlp_ready", return_value=True), patch.object(
            bootstrap, "embed_ready", return_value=False
        ), patch.object(bootstrap, "ffmpeg_ready", return_value=True), patch.object(
            bootstrap, "install_ytdlp", side_effect=broken
        ):
            errors = bootstrap.ensure_minimum()
        self.assertEqual(errors, [])

    def test_installs_both_components_in_parallel(self):
        names = []
        finished = threading.Event()
        lock = threading.Lock()

        def fake_ytdlp(log=None, progress=None):
            with lock:
                names.append(threading.current_thread().name)
            if progress:
                progress(1.0)

        def fake_ffmpeg(log=None, progress=None):
            with lock:
                names.append(threading.current_thread().name)
            if progress:
                progress(1.0)
            finished.set()

        fractions = []
        with patch.object(bootstrap, "ytdlp_ready", return_value=False), patch.object(
            bootstrap, "ffmpeg_ready", return_value=False
        ), patch.object(bootstrap, "install_ytdlp", side_effect=fake_ytdlp), patch.object(
            bootstrap, "install_ffmpeg", side_effect=fake_ffmpeg
        ):
            errors = bootstrap.ensure_minimum(progress=fractions.append, log=lambda _m: None)
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        self.assertIn("Bootstrap-yt-dlp", names)
        self.assertIn("Bootstrap-ffmpeg", names)
        self.assertEqual(fractions[-1], 1.0)

    def test_one_missing_component_runs_inline(self):
        calls = []
        with patch.object(bootstrap, "ytdlp_ready", return_value=False), patch.object(
            bootstrap, "ffmpeg_ready", return_value=True
        ), patch.object(bootstrap, "install_ytdlp", side_effect=lambda log=None, progress=None: calls.append("yt-dlp")), patch.object(
            bootstrap, "install_ffmpeg"
        ) as ffmpeg:
            self.assertEqual(bootstrap.ensure_minimum(), [])
        self.assertEqual(calls, ["yt-dlp"])
        ffmpeg.assert_not_called()

    def test_failure_keeps_going_and_reports_component(self):
        def broken(log=None, progress=None):
            raise RuntimeError("network down")

        with patch.object(bootstrap, "ytdlp_ready", return_value=False), patch.object(
            bootstrap, "ffmpeg_ready", return_value=True
        ), patch.object(bootstrap, "install_ytdlp", side_effect=broken):
            errors = bootstrap.ensure_minimum()
        self.assertEqual(len(errors), 1)
        self.assertIn("yt-dlp", errors[0])
        self.assertIn("network down", errors[0])


class PypiTests(unittest.TestCase):
    def test_wheel_returns_sha256(self):
        payload = {
            "info": {"version": "1.2.3"},
            "urls": [{
                "packagetype": "bdist_wheel",
                "filename": "demo-1.2.3-py3-none-any.whl",
                "url": "https://example.com/demo.whl",
                "digests": {"sha256": "a" * 64},
            }],
        }
        with patch.object(components, "_get_json", return_value=payload):
            version, url, digest = components._pypi_wheel("demo")
        self.assertEqual(version, "1.2.3")
        self.assertEqual(url, "https://example.com/demo.whl")
        self.assertEqual(digest, "a" * 64)


class ProgressWindowTests(unittest.TestCase):
    def test_closing_the_window_still_waits_for_installation(self):
        script = textwrap.dedent(
            """
            import sys
            import threading
            import time

            import bootstrap

            order = []
            try:
                window = bootstrap._ProgressWindow("test")
            except Exception:
                sys.exit(3)

            def worker(report):
                report("Устанавливаю yt-dlp...", 0.5)
                time.sleep(0.3)
                order.append("worker")

            def close():
                order.append("closed")
                window._root.destroy()

            assert window._root.protocol("WM_DELETE_WINDOW")
            window._root.after(50, close)
            errors = window.run(worker)
            assert errors == [], errors
            assert order == ["closed", "worker"], order
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        if result.returncode == 3:
            raise unittest.SkipTest("Tk display unavailable")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
