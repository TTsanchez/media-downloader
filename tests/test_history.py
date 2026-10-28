import json
import tempfile
import unittest
from pathlib import Path

from history import History


class HistoryTests(unittest.TestCase):
    def test_add_keeps_newest_first_and_respects_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            store = History(Path(folder) / "history.json", limit=2)
            for index in range(3):
                self.assertIsNotNone(store.add({
                    "url": f"https://example.com/{index}",
                    "title": f"Видео {index}",
                    "folder": folder,
                    "file": f"{index}.mp4",
                }))
            entries = store.entries()
            self.assertEqual(len(entries), 2)
            self.assertEqual([entry["title"] for entry in entries], ["Видео 2", "Видео 1"])
            self.assertTrue((Path(folder) / "history.json").is_file())

    def test_entry_without_file_or_folder_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            store = History(Path(folder) / "history.json")
            self.assertIsNone(store.add({"url": "https://example.com/x", "title": "Без пути"}))
            self.assertEqual(len(store), 0)

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            path.write_text("{not json", encoding="utf-8")
            store = History(path)
            self.assertEqual(store.entries(), [])

    def test_clear_empties_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.json"
            store = History(path)
            store.add({"url": "https://example.com/x", "folder": folder})
            store.clear()
            self.assertEqual(len(store), 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), [])

    def test_remove_deletes_single_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            store = History(Path(folder) / "history.json")
            first = store.add({"url": "https://example.com/1", "folder": folder})
            store.add({"url": "https://example.com/2", "folder": folder})
            store.remove(first["id"])
            self.assertEqual([entry["url"] for entry in store.entries()],
                             ["https://example.com/2"])


if __name__ == "__main__":
    unittest.main()
