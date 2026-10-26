import faulthandler
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import zipfile

import diagnostics
from downloader import YtDlpLogger


class RedactionTests(unittest.TestCase):
    def test_urls_headers_and_named_secrets(self):
        samples = (
            ("https://name:proxy-password@example.com/watch?signature=signed-secret#private-fragment",
             ("proxy-password", "signed-secret", "private-fragment")),
            ("Authorization: Bearer bearer-secret\nCookie: SID=cookie-secret", ("bearer-secret", "cookie-secret")),
            ('{"password": "json-password", "access_token": "access-secret"}', ("json-password", "access-secret")),
            ("SID=session-secret LOGIN_INFO=login-secret api_key=api-secret", ("session-secret", "login-secret", "api-secret")),
        )
        for message, secrets in samples:
            with self.subTest(message=message):
                output = diagnostics.redact(message)
                for secret in secrets:
                    self.assertNotIn(secret, output)
                self.assertIn("redacted", output)

    def test_custom_secret_and_home_path_are_hidden(self):
        secret = 'private-value-with-"quotes"'
        diagnostics.register_secret(secret)
        self.assertNotIn(secret, diagnostics.redact(secret))
        self.assertNotIn("private-value", diagnostics.redact(json.dumps({"value": secret})))
        self.assertNotIn(str(Path.home()), diagnostics.redact(str(Path.home() / "Downloads")))

    def test_proxy_password_is_hidden_outside_url(self):
        diagnostics.register_secret("http://user:secret-proxy-password@example.com:8080")
        self.assertNotIn("secret-proxy-password", diagnostics.redact("connection failed for secret-proxy-password"))

    def test_cookie_values_and_malformed_cookie_lines_are_hidden(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cookies.txt"
            row = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret-cookie-value"
            invalid = "invalid cookie row with secret-invalid-cookie-value"
            path.write_text(row + "\n" + invalid + "\n", encoding="utf-8")
            diagnostics.register_cookie_secrets(path)
            self.assertNotIn("secret-cookie-value", diagnostics.redact("parse error: " + repr(row)))
            self.assertNotIn("secret-cookie-value", diagnostics.redact("SID=" + "secret-cookie-value"))
            self.assertNotIn("secret-invalid-cookie-value", diagnostics.redact(repr(invalid)))


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service = diagnostics.Diagnostics(self.root / "logs", install_hooks=False, enable_faults=False)
        self.addCleanup(self.service.close)
        self.logger = logging.getLogger("media_downloader.test")

    def read_log(self):
        self.service.handler.flush()
        return self.service.log_file.read_text(encoding="utf-8")

    def test_traceback_and_metadata_are_saved_and_redacted(self):
        secret = "private-traceback-token"
        try:
            raise RuntimeError("https://example.com/video?token=" + secret)
        except RuntimeError:
            self.logger.exception("Download failed")
        text = self.read_log()
        self.assertIn("Traceback", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("Download failed", text)
        self.assertNotIn(secret, text)
        self.assertIn("app_version", text)
        self.assertIn("thread=MainThread", text)

    def test_export_contains_only_redacted_diagnostics(self):
        (self.service.directory / "cookies.txt").write_text("actual-cookie-content", encoding="utf-8")
        (self.service.directory / "config.json").write_text('{"password":"actual-password"}', encoding="utf-8")
        (self.service.directory / "media-downloader-previous.log").write_text(
            "Cookie: secret-from-old-session\nhttps://example.com/v?sig=old-signed-token", encoding="utf-8")
        self.logger.error("Authorization: Bearer fresh-secret")
        destination = self.service.export(self.root / "report.zip")
        with zipfile.ZipFile(destination) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertTrue(all(name.startswith("logs/") or name in ("README.txt", "system.json") for name in names))
            metadata = json.loads(archive.read("system.json"))
            self.assertEqual(metadata["session_id"], self.service.session_id)
            data = "\n".join(archive.read(name).decode("utf-8") for name in names)
        for secret in ("actual-cookie-content", "actual-password", "secret-from-old-session", "old-signed-token", "fresh-secret"):
            self.assertNotIn(secret, data)
        self.assertIn("previous.log", "\n".join(names))

    def test_external_process_failure_keeps_exit_code_and_output(self):
        code, output = diagnostics.run_process([
            sys.executable, "-c", "import sys; print('process-output'); sys.stderr.write('process-stderr'); sys.exit(7)",
        ])
        self.assertEqual(code, 7)
        self.assertIn("process-stderr", output)
        text = self.read_log()
        self.assertIn("rc=7", text)
        self.assertIn("process-output", text)
        self.assertIn("process-stderr", text)

    def test_process_timeout_is_reported(self):
        with patch.object(diagnostics.subprocess, "run", side_effect=subprocess.TimeoutExpired("command", 2, output="timeout-output")):
            code, output = diagnostics.run_process(["command"], timeout=2)
        self.assertEqual(code, 1)
        self.assertIn("Таймаут", output)
        self.assertIn("timeout-output", self.read_log())

    def test_missing_process_is_reported(self):
        code, _ = diagnostics.run_process([str(self.root / "does-not-exist.exe")])
        self.assertEqual(code, 1)
        self.assertIn("Process could not start", self.read_log())

    def test_yt_dlp_warnings_and_errors_reach_file(self):
        logger = YtDlpLogger(self.logger)
        logger.warning("Extractor warning")
        logger.error("Extractor error")
        self.assertIn("Extractor warning", self.read_log())
        self.assertIn("Extractor error", self.read_log())

    def test_fragmented_stderr_is_redacted_as_one_line(self):
        stream = diagnostics.LogStream(logging.ERROR)
        stream.write("Cookie: ")
        stream.write("unusual-cookie-name=fragmented-secret")
        stream.write("\n")
        self.assertNotIn("fragmented-secret", self.read_log())
        self.assertIn("Cookie: <redacted>", self.read_log())

    def test_unwritable_export_preserves_existing_report(self):
        destination = self.root / "report.zip"
        destination.write_bytes(b"previous-report")
        with patch.object(diagnostics.zipfile, "ZipFile", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.export(destination)
        self.assertEqual(destination.read_bytes(), b"previous-report")

    def test_disk_write_failure_retains_error_in_memory(self):
        with patch.object(self.service.handler, "shouldRollover", side_effect=OSError("disk full")):
            self.logger.error("message-that-must-survive-disk-failure")
        self.assertTrue(self.service.handler.storage_error)
        with zipfile.ZipFile(self.service.export(self.root / "emergency.zip")) as archive:
            self.assertIn("message-that-must-survive-disk-failure", archive.read("logs/storage-error.log").decode())


class HookTests(unittest.TestCase):
    def test_uncaught_thread_and_resource_errors_are_captured(self):
        original = (sys.excepthook, threading.excepthook, sys.unraisablehook)
        with tempfile.TemporaryDirectory() as folder:
            service = diagnostics.Diagnostics(folder, install_hooks=True, enable_faults=False)
            try:
                def fail():
                    raise RuntimeError("background-task-failure")
                worker = threading.Thread(target=fail, name="Broken-worker")
                worker.start()
                worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                sys.unraisablehook(types.SimpleNamespace(exc_type=ValueError, exc_value=ValueError("resource-failure"), exc_traceback=None))
                sys.excepthook(RuntimeError, RuntimeError("main-thread-failure"), None)
                text = service.log_file.read_text(encoding="utf-8")
                for value in ("background-task-failure", "Broken-worker", "resource-failure", "main-thread-failure"):
                    self.assertIn(value, text)
                self.assertEqual(service.notifications.qsize(), 3)
            finally:
                service.close()
            self.assertEqual((sys.excepthook, threading.excepthook, sys.unraisablehook), original)
            self.assertIn("Session ended after fatal error", service.log_file.read_text(encoding="utf-8"))

    def test_startup_import_failure_is_saved_without_gui(self):
        with tempfile.TemporaryDirectory() as folder:
            script = ("import sys; from diagnostics import initialize_logging; "
                      "initialize_logging(directory=sys.argv[1], notify_fatal=False); "
                      "import missing_media_downloader_dependency_for_test")
            result = subprocess.run([sys.executable, "-c", script, folder], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            text = "\n".join(file.read_text(encoding="utf-8") for file in Path(folder).glob("*.log"))
            self.assertIn("ModuleNotFoundError", text)
            self.assertIn("missing_media_downloader_dependency_for_test", text)

    def test_native_trace_has_its_own_file(self):
        if faulthandler.is_enabled():
            self.skipTest("An external fault handler is already installed")
        with tempfile.TemporaryDirectory() as folder:
            service = diagnostics.Diagnostics(folder, install_hooks=False, enable_faults=True)
            try:
                self.assertTrue(service._owns_fault_handler)
                faulthandler.dump_traceback(file=service._fault_handle, all_threads=True)
                self.assertIn("test_native_trace_has_its_own_file", service.fault_file.read_text(encoding="utf-8"))
            finally:
                service.close()


class StorageTests(unittest.TestCase):
    def test_rotation_keeps_bounded_number_of_files(self):
        with tempfile.TemporaryDirectory() as folder:
            service = diagnostics.Diagnostics(folder, install_hooks=False, enable_faults=False, max_bytes=800, backups=2)
            try:
                for i in range(50):
                    diagnostics.LOG.info("rotation-record-%s %s", i, "x" * 200)
                files = list(Path(folder).glob("*.log*"))
                self.assertLessEqual(len(files), 3)
                self.assertTrue(any("rotation-record-49" in path.read_text(encoding="utf-8") for path in files))
            finally:
                service.close()

    def test_inaccessible_preferred_directory_uses_temp_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            preferred = Path(folder) / "occupied"
            preferred.write_text("file, not directory")
            with patch.object(diagnostics.tempfile, "gettempdir", return_value=folder):
                service = diagnostics.Diagnostics(preferred, install_hooks=False, enable_faults=False)
            try:
                self.assertTrue(service.context["storage_fallback"])
                self.assertTrue(service.log_file.is_file())
                self.assertNotEqual(service.directory, preferred)
            finally:
                service.close()

    def test_memory_fallback_can_be_exported(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(Path, "mkdir", side_effect=PermissionError("blocked")):
                service = diagnostics.Diagnostics(Path(folder) / "blocked", install_hooks=False, enable_faults=False)
            try:
                self.assertIsInstance(service.handler, diagnostics.MemoryLog)
                diagnostics.LOG.error("memory-fallback-error")
                with zipfile.ZipFile(service.export(Path(folder) / "memory.zip")) as archive:
                    self.assertIn("memory-fallback-error", archive.read("logs/memory.log").decode())
            finally:
                service.close()

    def test_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(diagnostics, "_service", None):
            service = diagnostics.initialize_logging(directory=folder, install_hooks=False, enable_faults=False)
            try:
                self.assertIs(service, diagnostics.initialize_logging())
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
