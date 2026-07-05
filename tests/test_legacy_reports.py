"""Strict compatibility tests for unversioned v0.2 scan reports."""

from __future__ import annotations

import csv
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from music_manager.artifact_schema import LIBRARY_SCAN_HEADER
from music_manager.cli import main
from music_manager.reports import (
    ANALYSIS_REPORT_FILENAMES,
    CSV_FIELDNAMES,
    LEGACY_SCAN_FIELDNAMES,
    LegacyReportValidationError,
    read_legacy_scan_report,
)


def _legacy_row(**overrides: str) -> dict[str, str]:
    row = {
        "path": "Music/Artist/Album/01 Track.mp3",
        "extension": ".mp3",
        "file_type": "audio",
        "file_size_bytes": "123456",
        "folder_depth": "3",
        "artist": "Synthetic Artist",
        "title": "Synthetic Track",
        "album": "Synthetic Album",
        "date_year": "2026",
        "track_number": "1/10",
        "bitrate_kbps": "192.0",
        "duration_seconds": "180.125",
        "is_loose_track": "False",
        "is_archive": "False",
        "status": "ok",
        "error": "",
    }
    row.update(overrides)
    return row


def _write_report(
    path: Path,
    header: list[str],
    rows: list[dict[str, str]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=header)
        writer.writeheader()
        writer.writerows(
            {field_name: row[field_name] for field_name in header} for row in rows
        )


class LegacyReportReaderTests(unittest.TestCase):
    """Verify exact legacy shapes without fabricating schema 1 identity."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.report = self.directory / "library_scan.csv"

    def test_accepts_both_documented_headers_without_provenance(self) -> None:
        for header in (CSV_FIELDNAMES, LEGACY_SCAN_FIELDNAMES):
            with self.subTest(header=header):
                _write_report(self.report, header, [_legacy_row()])
                original = self.report.read_bytes()

                records = read_legacy_scan_report(self.report)

                self.assertEqual(len(records), 1)
                self.assertEqual(
                    records[0].path,
                    Path("Music/Artist/Album/01 Track.mp3"),
                )
                self.assertEqual(records[0].track_number, "1/10")
                self.assertIsNone(records[0].scan_id)
                self.assertIsNone(records[0].file_record_id)
                self.assertEqual(self.report.read_bytes(), original)

    def test_accepts_a_header_only_legacy_report(self) -> None:
        _write_report(self.report, CSV_FIELDNAMES, [])

        self.assertEqual(read_legacy_scan_report(self.report), [])

    def test_accepts_historical_value_variants_and_unusual_paths(self) -> None:
        unusual_path = 'C:\\Music, Archive\\Odd "Name" [mix].MP3'
        _write_report(
            self.report,
            LEGACY_SCAN_FIELDNAMES,
            [
                _legacy_row(
                    path=unusual_path,
                    extension=" .MP3 ",
                    file_type=" Audio ",
                    file_size_bytes=" 00123.0 ",
                    folder_depth="",
                    artist="Comma, Artist",
                    album="",
                    date_year="",
                    track_number="",
                    bitrate_kbps="1.92e2",
                    duration_seconds="",
                    is_loose_track="",
                    is_archive=" no ",
                    status=" OK ",
                )
            ],
        )

        records = read_legacy_scan_report(self.report, path_mode="absolute")

        self.assertEqual(len(records), 1)
        self.assertEqual(str(records[0].path), unusual_path)
        self.assertEqual(records[0].extension, ".mp3")
        self.assertEqual(records[0].file_size_bytes, 123)
        self.assertEqual(records[0].bitrate_kbps, 192.0)
        self.assertIsNone(records[0].duration_seconds)
        self.assertEqual(records[0].artist, "Comma, Artist")
        self.assertEqual(records[0].status, "ok")

    def test_windows_absolute_paths_are_private_by_default(self) -> None:
        private_root = "C:\\Users\\Alice\\Private Music\\Album"
        _write_report(
            self.report,
            CSV_FIELDNAMES,
            [
                _legacy_row(path=f"{private_root}\\01.mp3"),
                _legacy_row(
                    path=f"{private_root}\\02.mp3",
                    status="error",
                    error=f"could not read {private_root}\\02.mp3",
                ),
            ],
        )

        records = read_legacy_scan_report(self.report)

        self.assertEqual([str(record.path) for record in records], ["01.mp3", "02.mp3"])
        self.assertNotIn("C:\\", records[1].error)
        self.assertNotIn("Alice", records[1].error)

    def test_rejects_unknown_missing_duplicate_and_reordered_headers(self) -> None:
        header_cases = {
            "unknown": (
                [*CSV_FIELDNAMES, "unexpected"],
                "unknown columns: unexpected",
            ),
            "missing": (
                [name for name in CSV_FIELDNAMES if name != "title"],
                "missing required columns: title",
            ),
            "duplicate": (
                ["artist" if name == "title" else name for name in CSV_FIELDNAMES],
                "duplicate columns: artist",
            ),
            "reordered": (
                [
                    CSV_FIELDNAMES[1],
                    CSV_FIELDNAMES[0],
                    *CSV_FIELDNAMES[2:],
                ],
                "must exactly match a documented v0.2 header",
            ),
            "partial_extended": (
                [
                    *CSV_FIELDNAMES[:4],
                    "folder_depth",
                    *CSV_FIELDNAMES[4:],
                ],
                "missing required columns: is_loose_track",
            ),
            "schema_1": (
                list(LIBRARY_SCAN_HEADER),
                "unknown columns: scan_id",
            ),
        }
        for name, (header, message) in header_cases.items():
            with self.subTest(name=name):
                _write_report(self.report, header, [])

                with self.assertRaisesRegex(
                    LegacyReportValidationError,
                    message,
                ):
                    read_legacy_scan_report(self.report)

    def test_rejects_bad_row_widths(self) -> None:
        for name, cells, found in (
            ("short", [""] * (len(CSV_FIELDNAMES) - 1), 13),
            ("long", [""] * (len(CSV_FIELDNAMES) + 1), 15),
        ):
            with self.subTest(name=name):
                with self.report.open(
                    "w",
                    encoding="utf-8",
                    newline="",
                ) as report:
                    writer = csv.writer(report)
                    writer.writerow(CSV_FIELDNAMES)
                    writer.writerow(cells)

                with self.assertRaisesRegex(
                    LegacyReportValidationError,
                    rf"expected 14 columns, found {found}",
                ):
                    read_legacy_scan_report(self.report)

    def test_rejects_malformed_legacy_values_and_shapes(self) -> None:
        cases = {
            "empty_path": ({"path": ""}, "path must not be empty"),
            "fractional_file_size": (
                {"file_size_bytes": "123.5"},
                "file_size_bytes must be a non-negative integer",
            ),
            "nonfinite_bitrate": (
                {"bitrate_kbps": "NaN"},
                "bitrate_kbps must be a finite non-negative decimal",
            ),
            "negative_duration": (
                {"duration_seconds": "-1"},
                "duration_seconds must be a finite non-negative decimal",
            ),
            "invalid_boolean": (
                {"is_archive": "sometimes"},
                "is_archive must be a legacy boolean",
            ),
            "unknown_status": (
                {"status": "readable"},
                "status must be one of: error, ok",
            ),
            "unknown_file_type": (
                {"file_type": "video"},
                "file_type must be one of: archive, audio",
            ),
            "fractional_folder_depth": (
                {"folder_depth": "1.5"},
                "folder_depth must be a non-negative integer",
            ),
        }
        for name, (overrides, message) in cases.items():
            with self.subTest(name=name):
                _write_report(
                    self.report,
                    LEGACY_SCAN_FIELDNAMES,
                    [_legacy_row(**overrides)],
                )

                with self.assertRaisesRegex(
                    LegacyReportValidationError,
                    message,
                ):
                    read_legacy_scan_report(self.report)

    def test_rejects_malformed_csv_quoting(self) -> None:
        self.report.write_text(
            ",".join(CSV_FIELDNAMES) + '\n"unterminated',
            encoding="utf-8",
        )

        with self.assertRaises(csv.Error):
            read_legacy_scan_report(self.report)

    def test_refuses_legacy_mode_when_a_sibling_manifest_exists(self) -> None:
        _write_report(self.report, CSV_FIELDNAMES, [_legacy_row()])
        (self.directory / "scan_manifest.json").write_text(
            "{}\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            LegacyReportValidationError,
            r"use --scan-run for schema 1 analysis",
        ):
            read_legacy_scan_report(self.report)

    def test_does_not_open_or_stat_referenced_music_paths(self) -> None:
        missing_music = self.directory / "private" / "missing.mp3"
        _write_report(
            self.report,
            CSV_FIELDNAMES,
            [_legacy_row(path=str(missing_music))],
        )
        original_open = Path.open
        original_stat = Path.stat

        def guarded_open(path: Path, *args: object, **kwargs: object):
            if path == missing_music:
                self.fail("legacy reader opened a referenced music path")
            return original_open(path, *args, **kwargs)

        def guarded_stat(path: Path, *args: object, **kwargs: object):
            if path == missing_music:
                self.fail("legacy reader statted a referenced music path")
            return original_stat(path, *args, **kwargs)

        with (
            mock.patch("pathlib.Path.open", autospec=True, side_effect=guarded_open),
            mock.patch("pathlib.Path.stat", autospec=True, side_effect=guarded_stat),
        ):
            records = read_legacy_scan_report(self.report)

        self.assertEqual(len(records), 1)
        self.assertFalse(missing_music.exists())


class LegacyAnalysisCliTests(unittest.TestCase):
    """Verify warned, flat legacy output and default path privacy."""

    def test_flat_analysis_stays_unversioned_and_preserves_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            report = root / "library_scan.csv"
            private_root = root / "Private Library"
            _write_report(
                report,
                CSV_FIELDNAMES,
                [
                    _legacy_row(path=str(private_root / "Album" / "01.mp3")),
                    _legacy_row(
                        path=str(private_root / "Album" / "02.mp3"),
                        status="error",
                        error=(
                            "ValueError: could not read "
                            f"{private_root / 'Album' / '02.mp3'}"
                        ),
                    ),
                ],
            )
            original = report.read_bytes()
            output = root / "analysis"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    output,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(["analyze", "--scan-report", str(report)])

            self.assertEqual(exit_code, 0)
            self.assertEqual(report.read_bytes(), original)
            self.assertIn(
                "Compatibility mode: legacy v0.2 (unversioned)",
                stdout.getvalue(),
            )
            self.assertIn("no durable provenance", stderr.getvalue())
            self.assertIn("Rescan the library", stderr.getvalue())
            self.assertFalse((output / "scan_manifest.json").exists())

            for filename in ANALYSIS_REPORT_FILENAMES.values():
                report_path = output / filename
                self.assertTrue(report_path.is_file())
                contents = report_path.read_text(encoding="utf-8")
                self.assertNotIn(str(private_root), contents)
                with report_path.open(
                    encoding="utf-8",
                    newline="",
                ) as output_report:
                    fieldnames = csv.DictReader(output_report).fieldnames or []
                self.assertNotIn("scan_id", fieldnames)
                self.assertNotIn("file_record_id", fieldnames)


if __name__ == "__main__":
    unittest.main()
