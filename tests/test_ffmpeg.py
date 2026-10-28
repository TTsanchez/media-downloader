import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import ffmpeg_tools
import utils


class BundledToolTests(unittest.TestCase):
    def test_frozen_app_finds_tools_inside_bundle_without_path(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            binaries = bundle / "ffmpeg" / "bin"
            binaries.mkdir(parents=True)
            for name in ffmpeg_tools.TOOLS:
                (binaries / name).write_bytes(b"bundled executable")
            with patch.object(utils.sys, "_MEIPASS", folder, create=True), patch.object(
                utils.shutil, "which", return_value=None
            ):
                self.assertEqual(utils.find_ffmpeg(), str((binaries / "ffmpeg.exe").resolve()))
                self.assertEqual(utils.find_ffprobe(), str((binaries / "ffprobe.exe").resolve()))

    def test_smaller_shared_windows_archive_is_preferred(self):
        response = MagicMock()
        response.json.return_value = {"assets": [
            {"name": "ffmpeg-master-latest-winarm64-gpl.zip"},
            {
                "name": "ffmpeg-master-latest-win64-gpl.zip",
                "browser_download_url": "https://example.com/full.zip",
            },
            {
                "name": "ffmpeg-master-latest-win64-gpl-shared.zip",
                "browser_download_url": "https://example.com/shared.zip",
            },
        ]}
        with patch.object(utils.requests, "get", return_value=response):
            chosen = utils.get_ffmpeg_download()
        self.assertEqual(chosen["name"], "ffmpeg-master-latest-win64-gpl-shared.zip")
        self.assertEqual(chosen["url"], "https://example.com/shared.zip")

    def test_full_archive_is_fallback(self):
        response = MagicMock()
        response.json.return_value = {"assets": [
            {
                "name": "ffmpeg-master-latest-win64-gpl.zip",
                "browser_download_url": "https://example.com/full.zip",
            },
        ]}
        with patch.object(utils.requests, "get", return_value=response):
            self.assertEqual(utils.get_ffmpeg_download()["url"], "https://example.com/full.zip")


class InstallTests(unittest.TestCase):
    def make_archive(self, include_probe=True):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("package/bin/ffmpeg.exe", b"ffmpeg")
            if include_probe:
                archive.writestr("package/bin/ffprobe.exe", b"ffprobe")
            archive.writestr("package/bin/avcodec-62.dll", b"library")
            archive.writestr("package/LICENSE.txt", "GPL license text")
            archive.writestr("../../unrelated.txt", "must not be extracted")
        return stream.getvalue()

    def install(self, folder, data, digest=None):
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        info = {"name": "ffmpeg.zip", "url": "https://example.com/ffmpeg.zip", "size": len(data),
                "digest": digest or actual}

        def fake_download(_url, destination, log=None, progress=None, timeout=None,
                          expected_sha256=None):
            if expected_sha256 and expected_sha256 != actual:
                raise RuntimeError("Контрольная сумма не совпала")
            destination.write_bytes(data)
            if progress:
                progress(1.0)
            return destination

        with patch.object(ffmpeg_tools, "get_ffmpeg_download", return_value=info), patch.object(
            ffmpeg_tools.net, "download", side_effect=fake_download
        ):
            return ffmpeg_tools.install_ffmpeg(Path(folder) / "tools")

    def test_installs_tools_libraries_and_license_without_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            binaries = self.install(folder, self.make_archive())
            self.assertEqual((binaries / "ffmpeg.exe").read_bytes(), b"ffmpeg")
            self.assertEqual((binaries / "ffprobe.exe").read_bytes(), b"ffprobe")
            self.assertEqual((binaries / "avcodec-62.dll").read_bytes(), b"library")
            self.assertTrue((binaries.parent / "licenses" / "LICENSE.txt").is_file())
            self.assertTrue((binaries.parent / "licenses" / "SOURCE.txt").is_file())
            self.assertFalse((Path(folder) / "unrelated.txt").exists())

    def test_bad_checksum_does_not_install_files(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "сумма"):
                self.install(folder, self.make_archive(), digest="sha256:incorrect")
            self.assertFalse((Path(folder) / "tools").exists())

    def test_incomplete_archive_does_not_install_files(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "ffprobe"):
                self.install(folder, self.make_archive(include_probe=False))
            self.assertFalse((Path(folder) / "tools").exists())


if __name__ == "__main__":
    unittest.main()
