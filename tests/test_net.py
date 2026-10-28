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
