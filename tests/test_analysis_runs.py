"""Tests for schema 1 analysis provenance and transaction boundaries."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import UUID

from music_manager import __version__
from music_manager.analysis_runs import analyze_scan_run
from music_manager.artifact_schema import (
    ArtifactValidationError,
    load_scan_manifest,
    make_file_record_id,
    validate_artifact_set,
)
from music_manager.reports import (
    CORRUPT_FILES_HEADER,
    DUPLICATE_CANDIDATES_HEADER,
    LIBRARY_ANALYSIS_HEADER,
    MISSING_METADATA_HEADER,
    QUALITY_SUMMARY_HEADER,
)
from music_manager.scan_runs import create_scan_run


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
OTHER_SCAN_ID = UUID("87654321-4321-4abc-8def-1234567890ab")
DERIVED_NAMES = {
    "library_analysis",
    "duplicate_candidates",
    "missing_metadata",
    "corrupt_files",
    "quality_summary",
}
FILE_REPORTS = {
    "duplicate_candidates.csv",
    "missing_metadata.csv",
    "corrupt_files.csv",
}


class _FakeInfo:
    bitrate = 192000
    length = 180.0
    sample_rate = 44100
    bits_per_sample = 16
    channels = 2


class _FakeAudio:
    def __init__(self, tags: dict[str, list[str]]) -> None:
        self.tags = tags
        self.info = _FakeInfo()


def _clock(second: int):
    return lambda: datetime(
        2026,
        7,
        4,
        17,
        0,
        second,
        tzinfo=timezone.utc,
    )


def _copy_valid_run(root: Path, scan_id: UUID = SCAN_ID) -> Path:
    run_directory = root / str(scan_id)
    shutil.copytree(FIXTURES, run_directory)
    return run_directory


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as report:
        return list(csv.DictReader(report))


def _manifest_data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class AnalysisRunTests(unittest.TestCase):
    """Exercise versioned analysis generation and registration."""

    def test_complete_run_registers_full_provenance_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            original = load_scan_manifest(manifest_path)

            outcome = analyze_scan_run(
                run_directory,
                duration_tolerance=2.5,
                clock=_clock(1),
            )

            self.assertEqual(outcome.directory, run_directory)
            self.assertEqual(outcome.manifest.state, "complete")
            self.assertEqual(outcome.manifest.completed_at, original.completed_at)
            self.assertEqual(
                {
                    name
                    for name, entry in outcome.manifest.artifacts.items()
                    if entry.role == "derived"
                },
                DERIVED_NAMES,
            )
            for name in ("library_scan", "scan_errors"):
                self.assertEqual(
                    outcome.manifest.artifacts[name],
                    original.artifacts[name],
                )

            validated = validate_artifact_set(manifest_path)
            record_ids = {str(row.file_record_id) for row in validated.library_rows}
            expected_headers = {
                "library_analysis.csv": LIBRARY_ANALYSIS_HEADER,
                "duplicate_candidates.csv": DUPLICATE_CANDIDATES_HEADER,
                "missing_metadata.csv": MISSING_METADATA_HEADER,
                "corrupt_files.csv": CORRUPT_FILES_HEADER,
                "quality_summary.csv": QUALITY_SUMMARY_HEADER,
            }
            for filename, expected_header in expected_headers.items():
                report_path = run_directory / filename
                with report_path.open(encoding="utf-8", newline="") as report:
                    reader = csv.DictReader(report)
                    rows = list(reader)
                    self.assertEqual(tuple(reader.fieldnames or ()), expected_header)
                self.assertTrue(all(row["scan_id"] == str(SCAN_ID) for row in rows))
                if filename in FILE_REPORTS:
                    self.assertTrue(
                        all(row["file_record_id"] in record_ids for row in rows)
                    )
                else:
                    self.assertNotIn("file_record_id", expected_header)

            for name in DERIVED_NAMES:
                entry = validated.manifest.artifacts[name]
                report_path = run_directory / entry.filename
                self.assertEqual(entry.application_version, __version__)
                self.assertEqual(entry.generated_at, "2026-07-04T17:00:01Z")
                self.assertEqual(entry.configuration, {"duration_tolerance": 2.5})
                self.assertEqual(
                    entry.sha256,
                    hashlib.sha256(report_path.read_bytes()).hexdigest(),
                )
                self.assertEqual(entry.row_count, len(_read_rows(report_path)))

            for filename, header in (
                ("duplicate_candidates.csv", DUPLICATE_CANDIDATES_HEADER),
                ("missing_metadata.csv", MISSING_METADATA_HEADER),
                ("corrupt_files.csv", CORRUPT_FILES_HEADER),
            ):
                self.assertEqual(
                    (run_directory / filename).read_text(encoding="utf-8"),
                    f"{','.join(header)}\n",
                )
            self.assertFalse(any(run_directory.glob(".*.tmp")))

    def test_incomplete_run_remains_incomplete_and_needs_no_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            (source / "Unreadable.mp3").write_bytes(b"synthetic")
            times = iter(
                (
                    datetime(2026, 7, 4, 16, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 4, 16, 0, 1, tzinfo=timezone.utc),
                )
            )

            def fail_metadata(path: Path) -> object:
                raise OSError(f"cannot read metadata from {path}")

            scan = create_scan_run(
                source,
                root / "reports",
                metadata_loader=fail_metadata,
                scan_id_factory=lambda: SCAN_ID,
                clock=lambda: next(times),
            )
            shutil.rmtree(source)

            outcome = analyze_scan_run(scan.directory, clock=_clock(2))

            self.assertEqual(outcome.manifest.state, "incomplete")
            self.assertFalse(source.exists())
            corrupt_rows = _read_rows(scan.directory / "corrupt_files.csv")
            self.assertEqual(len(corrupt_rows), 1)
            self.assertEqual(corrupt_rows[0]["scan_id"], str(SCAN_ID))
            self.assertNotEqual(corrupt_rows[0]["file_record_id"], "")
            self.assertIn("metadata_read_failed", corrupt_rows[0]["error"])
            validate_artifact_set(scan.directory / "scan_manifest.json")

    def test_file_oriented_rows_carry_their_inventory_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            for filename in ("A.mp3", "B.mp3", "Missing.m4a"):
                (source / filename).write_bytes(b"synthetic")
            times = iter(
                (
                    datetime(2026, 7, 4, 16, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 7, 4, 16, 0, 1, tzinfo=timezone.utc),
                )
            )

            def metadata(path: Path) -> _FakeAudio:
                if path.name == "Missing.m4a":
                    return _FakeAudio(
                        {
                            "artist": ["Synthetic Artist"],
                            "title": ["Needs Review"],
                        }
                    )
                return _FakeAudio(
                    {
                        "artist": ["Synthetic Artist"],
                        "title": ["Duplicate Song"],
                        "album": ["Synthetic Album"],
                        "date": ["2026"],
                        "tracknumber": ["1"],
                    }
                )

            scan = create_scan_run(
                source,
                root / "reports",
                metadata_loader=metadata,
                scan_id_factory=lambda: SCAN_ID,
                clock=lambda: next(times),
            )
            analyze_scan_run(scan.directory, clock=_clock(8))
            artifacts = validate_artifact_set(scan.directory / "scan_manifest.json")
            ids_by_path = {
                row.path: str(row.file_record_id) for row in artifacts.library_rows
            }

            duplicate_rows = _read_rows(scan.directory / "duplicate_candidates.csv")
            self.assertEqual(len(duplicate_rows), 2)
            for row in duplicate_rows:
                self.assertEqual(row["file_record_id"], ids_by_path[row["path"]])

            missing_rows = _read_rows(scan.directory / "missing_metadata.csv")
            self.assertEqual(len(missing_rows), 1)
            self.assertEqual(missing_rows[0]["path"], "Missing.m4a")
            self.assertEqual(
                missing_rows[0]["file_record_id"],
                ids_by_path["Missing.m4a"],
            )

    def test_analysis_never_opens_or_stats_reported_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            reported_paths = {
                Path(row.path)
                for row in validate_artifact_set(
                    run_directory / "scan_manifest.json"
                ).library_rows
            }
            original_open = Path.open
            original_stat = Path.stat

            def is_reported_path(path: Path) -> bool:
                return any(
                    len(path.parts) >= len(reported.parts)
                    and path.parts[-len(reported.parts) :] == reported.parts
                    for reported in reported_paths
                )

            def guarded_open(path: Path, *args: object, **kwargs: object):
                if is_reported_path(path):
                    raise AssertionError(f"opened reported path: {path}")
                return original_open(path, *args, **kwargs)

            def guarded_stat(path: Path, *args: object, **kwargs: object):
                if is_reported_path(path):
                    raise AssertionError(f"statted reported path: {path}")
                return original_stat(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", guarded_open),
                mock.patch.object(Path, "stat", guarded_stat),
            ):
                analyze_scan_run(run_directory, clock=_clock(3))

    def test_running_and_failed_scans_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            running = _copy_valid_run(root)
            running_manifest = _manifest_data(running / "scan_manifest.json")
            running_manifest["state"] = "running"
            running_manifest["completed_at"] = None
            running_manifest["artifacts"] = {}
            running_manifest["counts"] = {
                "inventory_rows": 0,
                "info_findings": 0,
                "error_findings": 0,
                "fatal_findings": 0,
                "skipped_symlinks": 0,
            }
            _write_manifest(running / "scan_manifest.json", running_manifest)

            with self.assertRaisesRegex(
                ArtifactValidationError,
                "running.*cannot be analyzed",
            ):
                analyze_scan_run(running)

            source = root / "source"
            source.mkdir()
            with mock.patch(
                "music_manager.scan_runs.scan_library",
                side_effect=RuntimeError("synthetic fatal failure"),
            ):
                failed = create_scan_run(
                    source,
                    root / "failed-reports",
                    scan_id_factory=lambda: OTHER_SCAN_ID,
                    clock=iter(
                        (
                            datetime(
                                2026,
                                7,
                                4,
                                16,
                                0,
                                0,
                                tzinfo=timezone.utc,
                            ),
                            datetime(
                                2026,
                                7,
                                4,
                                16,
                                0,
                                1,
                                tzinfo=timezone.utc,
                            ),
                        )
                    ).__next__,
                )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "failed.*cannot be analyzed",
            ):
                analyze_scan_run(failed.directory)

    def test_mixed_and_mismatched_scan_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mismatched = _copy_valid_run(root, OTHER_SCAN_ID)
            manifest_path = mismatched / "scan_manifest.json"
            manifest = _manifest_data(manifest_path)
            manifest["scan_id"] = str(OTHER_SCAN_ID)
            _write_manifest(manifest_path, manifest)
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "does not match the manifest",
            ):
                analyze_scan_run(mismatched)

        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            library_path = run_directory / "library_scan.csv"
            with library_path.open(encoding="utf-8", newline="") as report:
                rows = list(csv.DictReader(report))
            rows[1]["scan_id"] = str(OTHER_SCAN_ID)
            rows[1]["file_record_id"] = str(
                make_file_record_id(OTHER_SCAN_ID, rows[1]["path"])
            )
            with library_path.open("w", encoding="utf-8", newline="") as report:
                writer = csv.DictWriter(
                    report,
                    fieldnames=rows[0].keys(),
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(rows)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = _manifest_data(manifest_path)
            manifest["artifacts"]["library_scan"]["sha256"] = hashlib.sha256(
                library_path.read_bytes()
            ).hexdigest()
            _write_manifest(manifest_path, manifest)

            with self.assertRaisesRegex(
                ArtifactValidationError,
                "does not match the manifest",
            ):
                analyze_scan_run(run_directory)

    def test_manifest_registration_failure_registers_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            original_manifest = manifest_path.read_bytes()
            real_replace = os.replace

            def fail_manifest_replace(source: Path, target: Path) -> None:
                if Path(target) == manifest_path:
                    raise OSError("injected manifest registration failure")
                real_replace(source, target)

            with (
                mock.patch(
                    "music_manager.analysis_runs.os.replace",
                    side_effect=fail_manifest_replace,
                ),
                self.assertRaisesRegex(OSError, "injected manifest"),
            ):
                analyze_scan_run(run_directory, clock=_clock(4))

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            manifest = load_scan_manifest(manifest_path)
            self.assertFalse(
                any(entry.role == "derived" for entry in manifest.artifacts.values())
            )
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            validate_artifact_set(manifest_path)

    def test_failed_reanalysis_leaves_no_old_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            analyze_scan_run(run_directory, clock=_clock(5))
            real_replace = os.replace
            manifest_replacements = 0

            def fail_final_manifest_replace(source: Path, target: Path) -> None:
                nonlocal manifest_replacements
                if Path(target) == manifest_path:
                    manifest_replacements += 1
                    if manifest_replacements == 2:
                        raise OSError("injected reanalysis registration failure")
                real_replace(source, target)

            with (
                mock.patch(
                    "music_manager.analysis_runs.os.replace",
                    side_effect=fail_final_manifest_replace,
                ),
                self.assertRaisesRegex(OSError, "injected reanalysis"),
            ):
                analyze_scan_run(run_directory, clock=_clock(6))

            manifest = load_scan_manifest(manifest_path)
            self.assertEqual(manifest.state, "complete")
            self.assertFalse(
                any(entry.role == "derived" for entry in manifest.artifacts.values())
            )
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            validate_artifact_set(manifest_path)

    def test_staging_failure_cleans_temps_and_preserves_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            original_manifest = manifest_path.read_bytes()
            from music_manager import analysis_runs

            real_stage = analysis_runs._stage_csv
            staged_reports = 0

            def fail_second_stage(*args: object, **kwargs: object):
                nonlocal staged_reports
                staged_reports += 1
                if staged_reports == 2:
                    raise OSError("injected staging failure")
                return real_stage(*args, **kwargs)

            with (
                mock.patch(
                    "music_manager.analysis_runs._stage_csv",
                    side_effect=fail_second_stage,
                ),
                self.assertRaisesRegex(OSError, "injected staging"),
            ):
                analyze_scan_run(run_directory, clock=_clock(7))

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            self.assertFalse(
                any(
                    entry.role == "derived"
                    for entry in load_scan_manifest(manifest_path).artifacts.values()
                )
            )


if __name__ == "__main__":
    unittest.main()
