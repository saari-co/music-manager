"""Tests for read-only discovery, metadata extraction, and reporting."""

from __future__ import annotations

import csv
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock
from uuid import UUID

from music_manager.reports import write_csv_report
from music_manager.scanner import scan_library


class _FakeInfo:
    bitrate = 320000
    length = 183.4567
    sample_rate = 48000
    bits_per_sample = 24
    channels = 2
    codec = "synthetic-codec"


class _FakeAudio:
    tags = {
        "artist": ["Test Artist"],
        "albumartist": ["Test Album Artist"],
        "title": ["Test Title"],
        "album": ["Test Album"],
        "date": ["2026-07-04"],
        "tracknumber": ["1/2"],
        "discnumber": ["1/1"],
        "genre": ["Test Genre"],
        "composer": ["Test Composer"],
        "compilation": [1],
    }
    info = _FakeInfo()


def _source_snapshot(source: Path) -> dict[str, tuple[str, object]]:
    snapshot: dict[str, tuple[str, object]] = {}
    for path in sorted(source.rglob("*"), key=lambda item: str(item)):
        relative_path = path.relative_to(source).as_posix()
        if path.is_symlink():
            snapshot[relative_path] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative_path] = ("directory", path.stat().st_mode)
        else:
            snapshot[relative_path] = ("file", path.read_bytes())
    return snapshot


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
            archive_record = next(
                record for record in result.records if record.file_type == "archive"
            )
            self.assertEqual(archive_record.container, "zip")
            self.assertTrue(archive_record.file_fingerprint.startswith("stat-v1:"))
            archive_row = archive_record.to_library_scan_row()
            self.assertEqual(archive_row.file_type, "archive")
            self.assertEqual(archive_row.container, "zip")

    def test_scan_records_are_schema_ready_without_writing_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            audio_path = source / "Artist" / "Album" / "01 Track.mp3"
            audio_path.parent.mkdir(parents=True)
            audio_path.write_bytes(b"synthetic audio")
            before = _source_snapshot(source)
            scan_id = UUID("12345678-1234-4abc-8def-1234567890ab")

            result = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id=scan_id,
            )

            self.assertEqual(_source_snapshot(source), before)
            self.assertEqual(result.scan_id, scan_id)
            self.assertEqual(len(result.records), 1)
            record = result.records[0]
            self.assertEqual(record.relative_path, "Artist/Album/01 Track.mp3")
            self.assertEqual(record.scan_id, scan_id)
            self.assertEqual(record.file_record_id.version, 5)
            self.assertTrue(record.file_fingerprint.startswith("stat-v1:"))
            self.assertEqual(record.album_artist, "Test Album Artist")
            self.assertEqual(record.date, "2026-07-04")
            self.assertEqual(record.release_year, 2026)
            self.assertEqual(record.track_number, "1")
            self.assertEqual(record.parsed_track_number, 1)
            self.assertEqual(record.track_total, 2)
            self.assertEqual(record.disc_number, 1)
            self.assertEqual(record.disc_total, 1)
            self.assertEqual(record.genre, "Test Genre")
            self.assertEqual(record.composer, "Test Composer")
            self.assertIs(record.is_compilation, True)
            self.assertEqual(record.codec, "synthetic-codec")
            self.assertEqual(record.container, "mp3")
            self.assertEqual(record.sample_rate_hz, 48000)
            self.assertEqual(record.bit_depth, 24)
            self.assertEqual(record.channels, 2)

            schema_row = result.to_library_scan_rows()[0]
            self.assertEqual(schema_row.scan_id, scan_id)
            self.assertEqual(schema_row.file_record_id, record.file_record_id)
            self.assertEqual(schema_row.path, record.relative_path)
            self.assertEqual(schema_row.track_number, 1)
            self.assertEqual(schema_row.track_total, 2)
            self.assertEqual(result.to_scan_error_rows(), ())

    def test_record_identity_is_scan_local_and_fingerprint_is_stat_based(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            audio_path = source / "Track.mp3"
            audio_path.write_bytes(b"AAAA")
            fixed_mtime = 1_700_000_000_123_456_789
            os.utime(audio_path, ns=(fixed_mtime, fixed_mtime))
            first_scan_id = UUID("12345678-1234-4abc-8def-1234567890ab")
            second_scan_id = UUID("87654321-4321-4abc-8def-1234567890ab")

            first = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id=first_scan_id,
            ).records[0]
            repeated = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id=first_scan_id,
            ).records[0]
            rescanned = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id=second_scan_id,
            ).records[0]

            self.assertEqual(first.file_record_id, repeated.file_record_id)
            self.assertNotEqual(first.file_record_id, rescanned.file_record_id)
            self.assertEqual(first.file_fingerprint, rescanned.file_fingerprint)

            audio_path.write_bytes(b"BBBB")
            os.utime(audio_path, ns=(fixed_mtime, fixed_mtime))
            preserved_stat = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id=first_scan_id,
            ).records[0]
            self.assertEqual(
                preserved_stat.file_fingerprint,
                first.file_fingerprint,
            )

            os.utime(audio_path, ns=(fixed_mtime + 1, fixed_mtime + 1))
            changed_stat = scan_library(
                source,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id=first_scan_id,
            ).records[0]
            self.assertNotEqual(
                changed_stat.file_fingerprint,
                first.file_fingerprint,
            )

    def test_symlinks_are_persisted_as_findings_without_target_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            outside = root / "outside"
            real_directory = source / "Real"
            real_directory.mkdir(parents=True)
            outside.mkdir()
            real_audio = real_directory / "Track.mp3"
            outside_audio = outside / "Outside.mp3"
            real_audio.write_bytes(b"inside")
            outside_audio.write_bytes(b"outside")
            os.symlink("Real", source / "Inside Link")
            os.symlink(outside, source / "Outside Link")
            os.symlink(source, source / "Cycle")
            os.symlink(outside_audio, source / "Outside.mp3")
            os.symlink(outside / "Missing.mp3", source / "Broken.mp3")
            before = _source_snapshot(source)
            loaded: list[Path] = []

            def load_metadata(path: Path) -> _FakeAudio:
                loaded.append(path)
                return _FakeAudio()

            result = scan_library(source, metadata_loader=load_metadata)

            self.assertEqual(_source_snapshot(source), before)
            self.assertEqual(loaded, [real_audio])
            self.assertEqual(
                [record.relative_path for record in result.records],
                ["Real/Track.mp3"],
            )
            expected_links = {
                "Broken.mp3",
                "Cycle",
                "Inside Link",
                "Outside Link",
                "Outside.mp3",
            }
            symlink_findings = [
                finding
                for finding in result.findings
                if finding.error_code == "symlink_skipped"
            ]
            self.assertEqual(
                {finding.path for finding in symlink_findings},
                expected_links,
            )
            self.assertTrue(
                all(finding.severity == "info" for finding in symlink_findings)
            )
            for finding in symlink_findings:
                self.assertNotIn(str(root), finding.path)
                self.assertNotIn(str(root), finding.message)
                self.assertNotIn(str(outside), finding.message)
            self.assertEqual(
                {row.path for row in result.to_scan_error_rows()},
                expected_links,
            )

    def test_lstat_symlink_check_catches_file_link_missed_by_discovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()
            target = root / "Target.mp3"
            target.write_bytes(b"outside")
            link = source / "Link.mp3"
            os.symlink(target, link)
            loaded: list[Path] = []

            with mock.patch(
                "music_manager.scanner.os.path.islink",
                return_value=False,
            ):
                result = scan_library(
                    source,
                    metadata_loader=lambda path: loaded.append(path),
                )

            self.assertEqual(loaded, [])
            self.assertEqual(result.records, [])
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.findings[0].path, "Link.mp3")
            self.assertEqual(
                result.findings[0].error_code,
                "symlink_skipped",
            )

    def test_stat_error_is_structured_and_skips_metadata_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            audio_path = source / "Unreadable.mp3"
            audio_path.write_bytes(b"synthetic")
            loaded: list[Path] = []
            original_lstat = Path.lstat

            def fail_audio_stat(path: Path):
                if path == audio_path:
                    raise PermissionError(f"cannot stat {path}")
                return original_lstat(path)

            with mock.patch.object(Path, "lstat", fail_audio_stat):
                result = scan_library(
                    source,
                    metadata_loader=lambda path: loaded.append(path),
                )

            self.assertEqual(loaded, [])
            record = result.records[0]
            self.assertEqual(record.status, "error")
            self.assertIsNone(record.file_size_bytes)
            self.assertIsNone(record.modified_time_ns)
            self.assertEqual(record.file_fingerprint, "")
            self.assertEqual(len(result.findings), 1)
            finding = result.findings[0]
            self.assertEqual(finding.stage, "stat")
            self.assertEqual(finding.error_code, "file_stat_failed")
            self.assertEqual(finding.file_record_id, record.file_record_id)
            self.assertNotIn(str(source), finding.message)
            self.assertEqual(
                result.to_library_scan_rows()[0].record_status,
                "error",
            )
            self.assertEqual(result.to_scan_error_rows()[0].stage, "stat")

    def test_directory_error_is_structured_with_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()

            def fail_walk(
                root: Path,
                *,
                topdown: bool,
                onerror,
                followlinks: bool,
            ) -> list[object]:
                self.assertEqual(root, source)
                self.assertTrue(topdown)
                self.assertFalse(followlinks)
                onerror(
                    PermissionError(
                        13,
                        "synthetic denial",
                        str(source / "Private"),
                    )
                )
                return []

            with mock.patch("music_manager.scanner.os.walk", side_effect=fail_walk):
                result = scan_library(source)

            self.assertEqual(result.records, [])
            self.assertEqual(len(result.directory_errors), 1)
            self.assertEqual(len(result.findings), 1)
            finding = result.findings[0]
            self.assertEqual(finding.path, "Private")
            self.assertEqual(finding.stage, "discovery")
            self.assertEqual(finding.severity, "error")
            self.assertEqual(finding.error_code, "directory_read_failed")
            self.assertNotIn(str(source), finding.message)
            self.assertEqual(
                result.to_scan_error_rows()[0].path,
                "Private",
            )

    def test_csv_report_uses_stable_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            audio_path = source / "Track.mp3"
            audio_path.touch()
            result = scan_library(source, metadata_loader=lambda path: _FakeAudio())
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
                all(not hasattr(record, "library_source") for record in result.records)
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
            self.assertEqual(len(result.findings), 1)
            self.assertEqual(result.findings[0].stage, "metadata")
            self.assertEqual(
                result.findings[0].error_code,
                "metadata_read_failed",
            )
            self.assertEqual(
                result.findings[0].file_record_id,
                result.records[0].file_record_id,
            )
            self.assertNotIn(str(source), result.findings[0].message)
            self.assertEqual(
                result.to_scan_error_rows()[0].severity,
                "error",
            )
            self.assertNotIn(str(source), report_row["error"])
            self.assertIn("Unreadable.mp3", report_row["error"])


if __name__ == "__main__":
    unittest.main()
