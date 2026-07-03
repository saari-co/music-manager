"""Tests for read-only discovery, metadata extraction, and reporting."""

from __future__ import annotations

import csv
import tempfile
import unittest
import wave
from pathlib import Path

from music_manager.reports import write_csv_report
from music_manager.scanner import scan_library


class _FakeInfo:
    bitrate = 320000
    length = 183.4567


class _FakeAudio:
    tags = {
        "artist": ["Test Artist"],
        "title": ["Test Title"],
        "album": ["Test Album"],
        "date": ["2026"],
        "tracknumber": ["1/2"],
    }
    info = _FakeInfo()


class ScannerTests(unittest.TestCase):
    """Exercise the scanner without using or distributing real audio."""

    def test_scan_library_discovers_supported_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            album = source / "Artist" / "Album"
            album.mkdir(parents=True)
            (album / "01 Track.mp3").touch()
            (album / "02 Track.FLAC").touch()
            (source / "Loose Track.m4a").touch()
            (source / "Archive.ZIP").touch()
            (source / "notes.txt").touch()

            result = scan_library(source, metadata_loader=lambda path: _FakeAudio())

            self.assertEqual(len(result.records), 4)
            self.assertEqual(result.summary.root_library_total, 3)
            self.assertEqual(result.summary.audio_count, 3)
            self.assertEqual(result.summary.archive_count, 1)
            self.assertEqual(result.summary.file_error_count, 0)

            audio_records = [
                record for record in result.records if record.file_type == "audio"
            ]
            self.assertTrue(
                all(record.artist == "Test Artist" for record in audio_records)
            )
            self.assertTrue(
                all(record.bitrate_kbps == 320.0 for record in audio_records)
            )

    def test_csv_report_uses_stable_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            audio_path = source / "Track.mp3"
            audio_path.touch()
            result = scan_library(
                source, metadata_loader=lambda path: _FakeAudio()
            )
            report_path = source / "output" / "scan.csv"

            write_csv_report(result.records, report_path)

            with report_path.open(encoding="utf-8", newline="") as report_file:
                rows = list(csv.DictReader(report_file))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["artist"], "Test Artist")
            self.assertEqual(rows[0]["extension"], ".mp3")
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["path"], "Track.mp3")
            self.assertNotIn(str(source), rows[0]["path"])
            self.assertNotIn("library_source", rows[0])
            self.assertNotIn("is_loose_track", rows[0])
            self.assertNotIn("folder_depth", rows[0])

    def test_scan_respects_source_relative_ignore_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            included = source / "Incoming"
            ignored = source / "Music" / "Media.localized"
            included.mkdir()
            ignored.mkdir(parents=True)
            (included / "Keep.mp3").touch()
            (ignored / "Ignore.mp3").touch()

            result = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                ignore_patterns=("Music/Media.localized",),
            )

            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.summary.root_library_total, 1)

    def test_app_named_folders_are_one_root_library(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            for folder_name in ("Apple Music", "iTunes", "Plex", "Incoming"):
                folder = source / folder_name
                folder.mkdir()
                (folder / f"{folder_name}.mp3").touch()

            result = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
            )

            self.assertEqual(result.summary.root_library_total, 4)
            self.assertTrue(
                all(
                    not hasattr(record, "library_source")
                    for record in result.records
                )
            )

    def test_generated_wav_is_read_by_mutagen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            audio_path = source / "Generated Silence.wav"
            with wave.open(str(audio_path), "wb") as audio_file:
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(8000)
                audio_file.writeframes(b"\x00\x00" * 800)

            result = scan_library(source)

            self.assertEqual(len(result.records), 1)
            self.assertEqual(result.records[0].status, "ok")
            self.assertEqual(result.records[0].bitrate_kbps, 128.0)
            self.assertAlmostEqual(
                result.records[0].duration_seconds or 0.0, 0.1, places=3
            )

    def test_metadata_error_is_non_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            (source / "Unreadable.mp3").touch()

            def raise_metadata_error(path: Path) -> None:
                raise ValueError(f"synthetic metadata failure at {path}")

            result = scan_library(source, metadata_loader=raise_metadata_error)
            report_path = source / "reports" / "scan.csv"
            write_csv_report(
                result.records,
                report_path,
                source=source,
            )
            with report_path.open(encoding="utf-8", newline="") as report_file:
                report_row = next(csv.DictReader(report_file))

            self.assertEqual(result.summary.file_error_count, 1)
            self.assertEqual(result.records[0].status, "error")
            self.assertIn("synthetic metadata failure", result.records[0].error)
            self.assertNotIn(str(source), report_row["error"])
            self.assertIn("Unreadable.mp3", report_row["error"])


if __name__ == "__main__":
    unittest.main()
