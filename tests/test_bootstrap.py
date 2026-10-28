# -*- coding: utf-8 -*-
import threading
import unittest
from unittest.mock import patch

import bootstrap
import components


class EnsureMinimumTests(unittest.TestCase):
    def test_missing_nothing_does_not_install(self):
        with patch.object(bootstrap, "ytdlp_ready", return_value=True), patch.object(
            bootstrap, "ffmpeg_ready", return_value=True
        ), patch.object(bootstrap, "install_ytdlp") as ytdlp, patch.object(
            bootstrap, "install_ffmpeg"
        ) as ffmpeg:
            self.assertEqual(bootstrap.ensure_minimum(), [])
        ytdlp.assert_not_called()
        ffmpeg.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
