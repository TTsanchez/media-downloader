import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import thumbnails


def _write_png(path: Path, size=(64, 36), color=(60, 120, 220)):
    from PIL import Image

    Image.new("RGB", size, color).save(path, format="PNG")


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.object(thumbnails, "data_root", return_value=self.root)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_guess_thumbnail_for_youtube_links(self):
        expected = "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?t=10",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
        ):
            self.assertEqual(thumbnails.guess_thumbnail(url), expected)
        self.assertEqual(thumbnails.guess_thumbnail("https://example.com/video"), "")

    def test_fetch_downloads_into_cache(self):
        url = "https://example.com/cover.jpg"

        def fake_download(_url, destination, **_kwargs):
            _write_png(Path(destination))

        with patch.object(thumbnails.net, "download", side_effect=fake_download) as download:
            path = thumbnails.fetch(url)
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        self.assertEqual(path, thumbnails.cached_path(url))
        download.assert_called_once()

    def test_fetch_reuses_cached_file(self):
        url = "https://example.com/cover.jpg"
        with patch.object(thumbnails.net, "download", side_effect=lambda _u, d, **_: _write_png(Path(d))):
            first = thumbnails.fetch(url)
        with patch.object(thumbnails.net, "download") as download:
            second = thumbnails.fetch(url)
        self.assertEqual(first, second)
        download.assert_not_called()

    def test_fetch_returns_none_on_network_error(self):
        with patch.object(thumbnails.net, "download", side_effect=OSError("offline")):
            self.assertIsNone(thumbnails.fetch("https://example.com/broken.jpg"))

    def test_fetch_rejects_non_http_urls(self):
        self.assertIsNone(thumbnails.fetch("file:///etc/passwd"))

    def test_load_fits_requested_size(self):
        path = self.root / "cover.png"
        _write_png(path, size=(120, 90))
        image = thumbnails.load(path, size=(112, 63), radius=8)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (112, 63))
        self.assertEqual(image.mode, "RGBA")

    def test_load_returns_none_for_missing_file(self):
        self.assertIsNone(thumbnails.load(self.root / "missing.png"))

    def test_placeholder_is_rgba(self):
        image = thumbnails.placeholder(size=(112, 63), radius=8)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (112, 63))
        self.assertEqual(image.mode, "RGBA")

    def test_prune_keeps_newest_files(self):
        folder = thumbnails.cache_dir()
        folder.mkdir(parents=True)
        for index in range(5):
            path = folder / f"{index}.img"
            path.write_bytes(b"x")
            path.touch()
            import os

            os.utime(path, (1000 + index, 1000 + index))
        removed = thumbnails.prune(limit=3)
        self.assertEqual(removed, 2)
        self.assertEqual(len(list(folder.iterdir())), 3)


if __name__ == "__main__":
    unittest.main()
