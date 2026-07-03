"""Synthetic CSV tests for the v0.2 library analysis layer."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Dict
from unittest import mock

from music_manager.analyzer import analyze_library
from music_manager.cli import main
from music_manager.reports import (
    ANALYSIS_REPORT_FILENAMES,
    CSV_FIELDNAMES,
    read_scan_report,
    write_analysis_reports,
)


def _scan_row(path: str, **overrides: str) -> Dict[str, str]:
    row = {
        "path": path,
        "extension": ".mp3",
        "file_type": "audio",
        "file_size_bytes": "1000",
        "artist": "Example Artist",
        "title": "Example Title",
        "album": "Example Album",
        "date_year": "2026",
        "track_number": "1",
        "bitrate_kbps": "192",
        "duration_seconds": "200",
        "is_archive": "False",
        "status": "ok",
        "error": "",
    }
    row.update(overrides)
    return row


class AnalyzerTests(unittest.TestCase):
    """Verify analysis without creating or accessing music files."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.scan_report = self.directory / "library_scan.csv"
        rows = [
            _scan_row(
                "collection/Album/01.mp3",
                artist="Example Artist",
                title="Same Song",
                bitrate_kbps="96",
                duration_seconds="200",
            ),
            _scan_row(
                "backup/Album/01.mp3",
                artist="  example   artist ",
                title="SAME SONG",
                bitrate_kbps="160",
                duration_seconds="202.9",
            ),
            _scan_row(
                "collection/Album/Alternate.mp3",
                artist="Example Artist",
                title="Same Song",
                bitrate_kbps="220",
                duration_seconds="206.5",
            ),
            _scan_row(
                "deep/library/Artist/Album/04.mp3",
                title="Quality 280",
                bitrate_kbps="280",
            ),
            _scan_row(
                "deep/library/Artist/Album/05.mp3",
                title="Quality 320",
                bitrate_kbps="320",
            ),
            _scan_row(
                "Loose Track.m4a",
                extension=".m4a",
                title="Needs Review",
                album="",
                date_year="",
                track_number="",
                bitrate_kbps="",
            ),
            _scan_row(
                "collection/Lossless.flac",
                extension=".flac",
                title="Lossless",
                bitrate_kbps="900",
            ),
            _scan_row(
                "too/deep/to/be/read/Unreadable.mp3",
                artist="",
                title="",
                album="",
                date_year="",
                track_number="",
                bitrate_kbps="",
                status="error",
                error="ValueError: synthetic corruption",
            ),
        ]
        with self.scan_report.open("w", encoding="utf-8", newline="") as report:
            writer = csv.DictWriter(report, fieldnames=CSV_FIELDNAMES)
            writer.writeheader()
            writer.writerows(rows)

        self.records = read_scan_report(self.scan_report)
        self.analysis = analyze_library(self.records)

    def test_duplicate_candidate_grouping(self) -> None:
        self.assertEqual(len(self.analysis.duplicate_groups), 1)
        group = self.analysis.duplicate_groups[0]
        self.assertEqual(group.group_id, "DUP-0001")
        self.assertEqual(len(group.records), 2)

    def test_missing_metadata_detection(self) -> None:
        self.assertEqual(len(self.analysis.missing_metadata), 1)
        finding = self.analysis.missing_metadata[0]
        self.assertEqual(str(finding.record.path), "Loose Track.m4a")
        self.assertEqual(
            finding.missing_fields,
            ("album", "date_year", "track_number"),
        )

    def test_corrupt_file_filtering(self) -> None:
        self.assertEqual(len(self.analysis.corrupt_files), 1)
        self.assertEqual(self.analysis.corrupt_files[0].status, "error")
        self.assertIn("synthetic corruption", self.analysis.corrupt_files[0].error)

    def test_bitrate_buckets(self) -> None:
        self.assertEqual(
            self.analysis.quality_buckets,
            {
                "unknown": 2,
                "under_128": 1,
                "128_to_191": 1,
                "192_to_255": 1,
                "256_to_319": 1,
                "320_plus": 1,
                "lossless_or_uncompressed": 1,
            },
        )
        self.assertEqual(self.analysis.summary.low_bitrate_files, 1)

    def test_root_library_total_includes_every_audio_row(self) -> None:
        self.assertEqual(self.analysis.summary.root_library_total, 8)
        self.assertEqual(self.analysis.summary.total_audio_files, 8)

    def test_metadata_completeness_percentages(self) -> None:
        self.assertEqual(self.analysis.metadata_completeness["artist"], 100.0)
        self.assertEqual(self.analysis.metadata_completeness["title"], 100.0)
        self.assertEqual(self.analysis.metadata_completeness["album"], 85.71)

    def test_all_analysis_reports_are_written(self) -> None:
        output_directory = self.directory / "reports"

        paths = write_analysis_reports(self.analysis, output_directory)

        self.assertEqual(set(paths), set(ANALYSIS_REPORT_FILENAMES))
        self.assertTrue(all(path.is_file() for path in paths.values()))
        with paths["duplicates"].open(encoding="utf-8", newline="") as report:
            duplicate_rows = list(csv.DictReader(report))
        self.assertEqual(len(duplicate_rows), 2)
        self.assertEqual(duplicate_rows[0]["duplicate_group_id"], "DUP-0001")
        self.assertNotIn("library_source", duplicate_rows[0])
        self.assertNotIn("folders", paths)
        self.assertFalse((output_directory / "folder_summary.csv").exists())
        for report_path in paths.values():
            report_text = report_path.read_text(encoding="utf-8")
            self.assertNotIn("library_source_summary", report_text)
            self.assertNotIn("loose_track_summary", report_text)
            with report_path.open(encoding="utf-8", newline="") as report:
                fieldnames = csv.DictReader(report).fieldnames or []
            self.assertNotIn("library_source", fieldnames)
            self.assertNotIn("is_loose_track", fieldnames)

        with paths["analysis"].open(encoding="utf-8", newline="") as report:
            analysis_rows = list(csv.DictReader(report))
        metrics = {row["metric"] for row in analysis_rows}
        self.assertIn("root_library_total", metrics)
        self.assertNotIn("library_sources", metrics)
        self.assertNotIn("loose_tracks", metrics)

    def test_analysis_cli_writes_reports_and_summary(self) -> None:
        output_directory = self.directory / "cli-reports"
        stdout = io.StringIO()

        with mock.patch(
            "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
            output_directory,
        ), redirect_stdout(stdout):
            exit_code = main(
                ["analyze", "--scan-report", str(self.scan_report)]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("Root Library total: 8", stdout.getvalue())
        self.assertIn("Duplicate candidate groups: 1", stdout.getvalue())
        self.assertIn("Duplicate candidate files: 2", stdout.getvalue())
        self.assertNotIn("Library sources", stdout.getvalue())
        self.assertNotIn("Loose tracks", stdout.getvalue())
        for filename in ANALYSIS_REPORT_FILENAMES.values():
            self.assertTrue((output_directory / filename).is_file())


if __name__ == "__main__":
    unittest.main()
