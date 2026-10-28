# -*- coding: utf-8 -*-
import unittest

import release_notes

CHANGELOG = """# Журнал изменений

## [Unreleased]

- не выпущено

## [1.2.0] — 2026-10-28

### Добавлено

- Компоненты доустанавливаются при первом запуске.

### Исправлено

- Кракозябры.

## [1.1.3] — 2026-10-26

- Старое.
"""


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_requested_version(self):
        notes = release_notes.extract_notes(CHANGELOG, "1.2.0")
        self.assertIn("Компоненты доустанавливаются", notes)
        self.assertIn("Кракозябры", notes)
        self.assertNotIn("Старое", notes)
        self.assertNotIn("не выпущено", notes)
        self.assertNotIn("## [1.1.3]", notes)
        self.assertFalse(notes.startswith("## ["))

    def test_accepts_v_prefix_and_date(self):
        notes = release_notes.extract_notes(CHANGELOG, "v1.2.0")
        self.assertIn("Компоненты доустанавливаются", notes)

    def test_missing_version_returns_empty(self):
        self.assertEqual(release_notes.extract_notes(CHANGELOG, "9.9.9"), "")

    def test_empty_version_returns_empty(self):
        self.assertEqual(release_notes.extract_notes(CHANGELOG, ""), "")


if __name__ == "__main__":
    unittest.main()
