# -*- coding: utf-8 -*-
import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import requests

import net


class FakeResponse:
    def __init__(self, chunks, status_code=200, headers=None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, _size):
        yield from self._chunks


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}
        self.last_headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, _url, stream=True, timeout=None, headers=None):
        self.last_headers = dict(headers or {})
        return self.response


class DownloadTests(unittest.TestCase):
    def test_resumes_partial_file_with_range(self):
        payload = b"abcdef"
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            destination.with_suffix(".bin.part").write_bytes(b"abc")
            response = FakeResponse([b"def"], status_code=206,
                                    headers={"Content-Range": "bytes 3-5/6"})
            fake = FakeSession(response)
            fractions = []
            with patch.object(net, "session", return_value=fake):
                net.download("https://example.com/file", destination,
                             progress=fractions.append)
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(fake.last_headers.get("Range"), "bytes=3-")
            self.assertEqual(fractions[-1], 1.0)

    def test_restarts_when_server_ignores_range(self):
        payload = b"abcdef"
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            destination.with_suffix(".bin.part").write_bytes(b"abc")
            response = FakeResponse([payload], status_code=200,
                                    headers={"Content-Length": str(len(payload))})
            with patch.object(net, "session", return_value=FakeSession(response)):
                net.download("https://example.com/file", destination)
            self.assertEqual(destination.read_bytes(), payload)

    def test_checksum_mismatch_removes_download(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            response = FakeResponse([b"data"], headers={"Content-Length": "4"})
            with patch.object(net, "session", return_value=FakeSession(response)):
                with self.assertRaises(net.ChecksumError):
                    net.download("https://example.com/file", destination,
                                 expected_sha256="0" * 64)
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_suffix(".bin.part").exists())

    def test_complete_partial_file_is_kept_without_request(self):
        payload = b"verified"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            destination.with_suffix(".bin.part").write_bytes(payload)
            with patch.object(net, "session") as session:
                net.download("https://example.com/file", destination,
                             expected_sha256="sha256:" + digest)
            session.assert_not_called()
            self.assertEqual(destination.read_bytes(), payload)


class SequenceSession:
    """Отдаёт заранее подготовленные ответы и запоминает заголовки запросов."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = []
        self.headers_seen = self.headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, _url, stream=True, timeout=None, headers=None):
        self.headers.append(dict(headers or {}))
        return self.responses.pop(0)


class SlowLinkTests(unittest.TestCase):
    def test_reconnects_and_resumes_when_speed_drops(self):
        clock = iter([0.0, 11.0, 12.0, 13.0])
        first = FakeResponse([b"ab"], status_code=206,
                             headers={"Content-Range": "bytes 0-3/4"})
        second = FakeResponse([b"cd"], status_code=206,
                              headers={"Content-Range": "bytes 2-3/4"})
        fake = SequenceSession([first, second])
        messages = []
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            with patch.object(net, "session", return_value=fake), \
                    patch.object(net, "STALL_WINDOW", 10), \
                    patch.object(net, "STALL_SPEED", 1000), \
                    patch.object(net.time, "sleep"), \
                    patch.object(net.time, "monotonic", side_effect=lambda: next(clock)):
                net.download("https://example.com/file", destination, log=messages.append)
            self.assertEqual(destination.read_bytes(), b"abcd")
        self.assertEqual(fake.headers[-1].get("Range"), "bytes=2-")
        self.assertEqual(len(fake.headers), 2)
        self.assertTrue(any("слишком медленное" in message for message in messages))

    def test_keeps_slow_stream_after_reconnect_limit(self):
        clock = iter([0.0, 11.0, 12.0])
        response = FakeResponse([b"ab", b"cd"], headers={"Content-Length": "4"})
        fake = SequenceSession([response])
        messages = []
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            with patch.object(net, "session", return_value=fake), \
                    patch.object(net, "STALL_WINDOW", 10), \
                    patch.object(net, "STALL_SPEED", 1000), \
                    patch.object(net, "STALL_RECONNECTS", 0), \
                    patch.object(net.time, "sleep"), \
                    patch.object(net.time, "monotonic", side_effect=lambda: next(clock)):
                net.download("https://example.com/file", destination, log=messages.append)
            self.assertEqual(destination.read_bytes(), b"abcd")
        self.assertEqual(len(fake.headers), 1)
        self.assertFalse(any("переподключаюсь" in message for message in messages))

    def test_does_not_reconnect_when_download_is_complete(self):
        clock = iter([0.0, 11.0])
        response = FakeResponse([b"abcd"], headers={"Content-Length": "4"})
        fake = SequenceSession([response])
        messages = []
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            with patch.object(net, "session", return_value=fake), \
                    patch.object(net, "STALL_WINDOW", 10), \
                    patch.object(net, "STALL_SPEED", 1000), \
                    patch.object(net.time, "sleep"), \
                    patch.object(net.time, "monotonic", side_effect=lambda: next(clock)):
                net.download("https://example.com/file", destination, log=messages.append)
            self.assertEqual(destination.read_bytes(), b"abcd")
        self.assertEqual(len(fake.headers), 1)
        self.assertFalse(any("переподключаюсь" in message for message in messages))

    def test_retries_after_network_error(self):
        failed = FakeResponse([], status_code=500)
        good = FakeResponse([b"data"], headers={"Content-Length": "4"})
        fake = SequenceSession([failed, good])
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "file.bin"
            with patch.object(net, "session", return_value=fake), \
                    patch.object(net.time, "sleep"):
                net.download("https://example.com/file", destination)
            self.assertEqual(destination.read_bytes(), b"data")
        self.assertEqual(len(fake.headers), 2)


class ZipSafetyTests(unittest.TestCase):
    def _archive(self, folder, name):
        path = Path(folder) / "archive.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(name, b"x")
        return path

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = self._archive(folder, "../../evil.txt")
            with self.assertRaisesRegex(RuntimeError, "Небезопасный путь"):
                net.safe_extract_zip(archive, Path(folder) / "out")
            self.assertFalse((Path(folder) / "evil.txt").exists())

    def test_extracts_normal_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "archive.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("folder/file.txt", b"ok")
            target = Path(folder) / "out"
            net.safe_extract_zip(archive, target)
            self.assertEqual((target / "folder" / "file.txt").read_bytes(), b"ok")


if __name__ == "__main__":
    unittest.main()
