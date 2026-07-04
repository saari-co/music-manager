"""Tests for privacy defaults and YAML-backed configuration."""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from music_manager.config import load_config
from music_manager.reports import (
    LEGACY_SCAN_FIELDNAMES,
    read_scan_report,
)


class ConfigTests(unittest.TestCase):
    def test_loads_supported_yaml_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.yml"
            config_path.write_text(
                "path_mode: absolute\n"
                "ignore:\n"
                "  - Music/Media.localized\n"
                "  - .DS_Store\n",
                encoding="utf-8",
            )

            config = load_config(config_path)

            self.assertEqual(config.path_mode, "absolute")
            self.assertEqual(
                config.ignore,
                ("Music/Media.localized", ".DS_Store"),
            )

    def test_rejects_unknown_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.yml"
            config_path.write_text(
                "path_mode: relative\n"
                "ignore:\n"
                "  - .DS_Store\n"
                "unexpected_setting: true\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                r"unknown configuration key: unexpected_setting",
            ):
                load_config(config_path)

    def test_rejects_invalid_path_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "config.yml"
            config_path.write_text("path_mode: private\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "path_mode"):
                load_config(config_path)

    def test_legacy_absolute_paths_are_relative_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            report_path = root / "legacy_scan.csv"
            rows = [
                self._legacy_row(root / "Apple Music" / "Album" / "01.mp3"),
                self._legacy_row(root / "Incoming" / "Album" / "01.mp3"),
            ]
            rows[1]["status"] = "error"
            rows[1]["error"] = f"cannot read {rows[1]['path']}"
            with report_path.open("w", encoding="utf-8", newline="") as report:
                writer = csv.DictWriter(
                    report, fieldnames=LEGACY_SCAN_FIELDNAMES
                )
                writer.writeheader()
                writer.writerows(rows)

            relative_records = read_scan_report(report_path)
            absolute_records = read_scan_report(
                report_path, path_mode="absolute"
            )
            self.assertTrue(
                all(not record.path.is_absolute() for record in relative_records)
            )
            self.assertTrue(
                all(record.path.is_absolute() for record in absolute_records)
            )
            self.assertNotIn(
                str(root),
                relative_records[1].error,
            )
            self.assertIn(
                "Incoming/Album/01.mp3",
                relative_records[1].error,
            )
    @staticmethod
    def _legacy_row(path: Path) -> dict:
        return {
            "path": str(path),
            "extension": ".mp3",
            "file_type": "audio",
            "file_size_bytes": "1000",
            "folder_depth": "2",
            "artist": "Synthetic Artist",
            "title": "Synthetic Title",
            "album": "Synthetic Album",
            "date_year": "2026",
            "track_number": "1",
            "bitrate_kbps": "192",
            "duration_seconds": "180",
            "is_loose_track": "False",
            "is_archive": "False",
            "status": "ok",
            "error": "",
        }


if __name__ == "__main__":
    unittest.main()
