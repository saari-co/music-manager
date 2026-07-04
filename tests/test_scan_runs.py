"""Tests for versioned scan run directories and manifest transitions."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import UUID

from music_manager.artifact_schema import (
    SCAN_ERRORS_HEADER,
    load_scan_manifest,
    validate_artifact_set,
)
from music_manager.scan_runs import create_scan_run


FIRST_SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
SECOND_SCAN_ID = UUID("87654321-4321-4abc-8def-1234567890ab")


class _FakeInfo:
    bitrate = 256000
    length = 123.5
    sample_rate = 48000
    bits_per_sample = 24
    channels = 2
    codec = "synthetic-codec"


class _FakeAudio:
    tags = {
        "artist": ["Test Artist"],
        "title": ["Test Title"],
        "album": ["Test Album"],
        "date": ["2026"],
        "tracknumber": ["1"],
    }
    info = _FakeInfo()


def _clock(*seconds: int):
    values = iter(
        datetime(2026, 7, 4, 16, 0, second, tzinfo=timezone.utc) for second in seconds
    )
    return lambda: next(values)


def _source_snapshot(source: Path) -> dict[str, tuple[str, object]]:
    snapshot: dict[str, tuple[str, object]] = {}
    for path in sorted(source.rglob("*"), key=lambda item: str(item)):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", path.stat().st_mode)
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


class ScanRunTests(unittest.TestCase):
    """Exercise durable primary artifact lifecycle behavior."""

    def test_complete_run_is_valid_deterministic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "private-library"
            ignored = source / "Ignored"
            source.mkdir()
            ignored.mkdir()
            (source / "B.mp3").write_bytes(b"B")
            (source / "a.mp3").write_bytes(b"A")
            (ignored / "ignored.mp3").write_bytes(b"ignored")
            os.symlink(root / "outside.mp3", source / "linked.mp3")
            before = _source_snapshot(source)

            outcome = create_scan_run(
                source,
                root / "reports",
                ignore_patterns=("Ignored",),
                metadata_loader=lambda path: _FakeAudio(),
                scan_id_factory=lambda: FIRST_SCAN_ID,
                clock=_clock(0, 1),
            )

            self.assertEqual(outcome.state, "complete")
            self.assertEqual(
                outcome.directory,
                root / "reports" / str(FIRST_SCAN_ID),
            )
            self.assertEqual(_source_snapshot(source), before)
            self.assertFalse((root / "reports" / "latest").exists())

            artifacts = validate_artifact_set(outcome.directory / "scan_manifest.json")
            self.assertEqual(
                [row.path for row in artifacts.library_rows],
                ["a.mp3", "B.mp3"],
            )
            self.assertEqual(len(artifacts.error_rows), 1)
            self.assertEqual(
                artifacts.error_rows[0].error_code,
                "symlink_skipped",
            )
            self.assertEqual(artifacts.manifest.counts.skipped_symlinks, 1)
            self.assertEqual(
                artifacts.manifest.configuration.ignore,
                ("Ignored",),
            )

            for path in outcome.directory.iterdir():
                if path.is_file():
                    self.assertNotIn(
                        str(root),
                        path.read_text(encoding="utf-8"),
                    )

    def test_complete_run_writes_header_only_scan_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()
            (source / "Track.mp3").write_bytes(b"synthetic")

            outcome = create_scan_run(
                source,
                root / "reports",
                metadata_loader=lambda path: _FakeAudio(),
                scan_id_factory=lambda: FIRST_SCAN_ID,
                clock=_clock(0, 1),
            )

            errors_path = outcome.directory / "scan_errors.csv"
            self.assertEqual(outcome.state, "complete")
            self.assertTrue(errors_path.is_file())
            self.assertEqual(
                errors_path.read_text(encoding="utf-8"),
                f"{','.join(SCAN_ERRORS_HEADER)}\n",
            )

            artifacts = validate_artifact_set(outcome.directory / "scan_manifest.json")
            self.assertEqual(artifacts.error_rows, ())
            self.assertEqual(
                artifacts.manifest.artifacts["scan_errors"].row_count,
                0,
            )
            self.assertEqual(artifacts.manifest.counts.info_findings, 0)
            self.assertEqual(artifacts.manifest.counts.error_findings, 0)
            self.assertEqual(artifacts.manifest.counts.fatal_findings, 0)

    def test_existing_scan_directory_is_never_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            reports = root / "reports"
            existing = reports / str(FIRST_SCAN_ID)
            source.mkdir()
            existing.mkdir(parents=True)
            marker = existing / "marker"
            marker.write_text("preserve", encoding="utf-8")
            scan_ids = iter((FIRST_SCAN_ID, SECOND_SCAN_ID))

            outcome = create_scan_run(
                source,
                reports,
                metadata_loader=lambda path: _FakeAudio(),
                scan_id_factory=lambda: next(scan_ids),
                clock=_clock(0, 1),
            )

            self.assertEqual(outcome.directory, reports / str(SECOND_SCAN_ID))
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            validate_artifact_set(outcome.directory / "scan_manifest.json")

    def test_metadata_error_produces_incomplete_consumable_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()
            audio_path = source / "Unreadable.mp3"
            audio_path.write_bytes(b"synthetic")

            def fail_metadata(path: Path) -> object:
                raise OSError(f"cannot read metadata from {path}")

            outcome = create_scan_run(
                source,
                root / "reports",
                metadata_loader=fail_metadata,
                scan_id_factory=lambda: FIRST_SCAN_ID,
                clock=_clock(0, 1),
            )

            self.assertEqual(outcome.state, "incomplete")
            artifacts = validate_artifact_set(outcome.directory / "scan_manifest.json")
            self.assertEqual(len(artifacts.library_rows), 1)
            self.assertEqual(artifacts.library_rows[0].record_status, "error")
            self.assertEqual(len(artifacts.error_rows), 1)
            self.assertEqual(artifacts.error_rows[0].severity, "error")
            self.assertNotIn(
                str(root),
                (outcome.directory / "scan_errors.csv").read_text(encoding="utf-8"),
            )

    def test_fatal_scan_error_produces_failed_nonconsumable_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "private-source"
            source.mkdir()

            with mock.patch(
                "music_manager.scan_runs.scan_library",
                side_effect=RuntimeError(f"fatal scan of {source}"),
            ):
                outcome = create_scan_run(
                    source,
                    root / "reports",
                    scan_id_factory=lambda: FIRST_SCAN_ID,
                    clock=_clock(0, 1),
                )

            self.assertEqual(outcome.state, "failed")
            self.assertFalse((outcome.directory / "library_scan.csv").exists())
            artifacts = validate_artifact_set(outcome.directory / "scan_manifest.json")
            self.assertEqual(len(artifacts.error_rows), 1)
            self.assertEqual(artifacts.error_rows[0].severity, "fatal")
            self.assertNotIn(str(root), artifacts.error_rows[0].message)

    def test_interrupted_run_remains_running_and_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()

            with (
                mock.patch(
                    "music_manager.scan_runs.scan_library",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertRaises(KeyboardInterrupt),
            ):
                create_scan_run(
                    source,
                    root / "reports",
                    scan_id_factory=lambda: FIRST_SCAN_ID,
                    clock=_clock(0),
                )

            directory = root / "reports" / str(FIRST_SCAN_ID)
            manifest = load_scan_manifest(directory / "scan_manifest.json")
            self.assertEqual(manifest.state, "running")
            self.assertFalse((directory / "library_scan.csv").exists())
            self.assertFalse((directory / "scan_errors.csv").exists())
            validate_artifact_set(directory / "scan_manifest.json")

    def test_final_manifest_replace_failure_cleans_partial_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()
            (source / "Track.mp3").write_bytes(b"synthetic")
            real_replace = os.replace
            manifest_replacements = 0

            def fail_first_final_manifest(source_path: Path, target: Path) -> None:
                nonlocal manifest_replacements
                if Path(target).name == "scan_manifest.json":
                    manifest_replacements += 1
                    if manifest_replacements == 2:
                        raise OSError("injected final manifest failure")
                real_replace(source_path, target)

            with mock.patch(
                "music_manager.scan_runs.os.replace",
                side_effect=fail_first_final_manifest,
            ):
                outcome = create_scan_run(
                    source,
                    root / "reports",
                    metadata_loader=lambda path: _FakeAudio(),
                    scan_id_factory=lambda: FIRST_SCAN_ID,
                    clock=_clock(0, 1, 2),
                )

            self.assertEqual(outcome.state, "failed")
            self.assertFalse((outcome.directory / "library_scan.csv").exists())
            self.assertFalse(any(outcome.directory.glob(".*.tmp")))
            validate_artifact_set(outcome.directory / "scan_manifest.json")

    def test_absolute_path_configuration_is_rejected_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            reports = root / "reports"
            source.mkdir()

            with self.assertRaisesRegex(
                ValueError,
                "schema 1 rejects absolute path output",
            ):
                create_scan_run(
                    source,
                    reports,
                    path_mode="absolute",
                )

            self.assertFalse(reports.exists())

    def test_manifest_json_contains_only_sanitized_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()

            outcome = create_scan_run(
                source,
                root / "reports",
                ignore_patterns=("Cache/*.mp3",),
                metadata_loader=lambda path: _FakeAudio(),
                scan_id_factory=lambda: FIRST_SCAN_ID,
                clock=_clock(0, 1),
            )
            manifest_data = json.loads(
                (outcome.directory / "scan_manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(
                manifest_data["configuration"],
                {
                    "ignore": ["Cache/*.mp3"],
                    "path_mode": "relative",
                    "follow_symlinks": False,
                },
            )
            self.assertNotIn(str(source), json.dumps(manifest_data))


if __name__ == "__main__":
    unittest.main()
