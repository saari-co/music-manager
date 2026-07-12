"""Tests for approval parsing and plan-only staging registration."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from unittest import mock
from uuid import UUID, uuid5

from music_manager.analysis_runs import analyze_scan_run
from music_manager.artifact_schema import (
    LIBRARY_SCAN_HEADER,
    SCAN_ERRORS_HEADER,
    STAGING_COPIES_HEADER,
    STAGING_ERRORS_HEADER,
    STAGING_SCHEMA_VERSION,
    ArtifactValidationError,
    LibraryScanRow,
    ScanErrorRow,
    StagingCopyRow,
    StagingErrorRow,
    load_scan_manifest,
    make_file_fingerprint,
    make_file_record_id,
    validate_artifact_set,
)
from music_manager.staging_plans import (
    APPROVAL_HEADER,
    create_staging_plan,
    load_approval_file,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
STAGE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_SCAN_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
AUDIO_ID = UUID("aec6a2b3-b8d7-55ea-a953-25d4c1a793dd")
ARCHIVE_ID = UUID("303a5d3d-9cd5-5402-96fb-3c19092287ec")


def _clock() -> datetime:
    return datetime(2026, 7, 11, 20, 0, tzinfo=timezone.utc)


def _copy_run(root: Path) -> Path:
    run_directory = root / str(SCAN_ID)
    shutil.copytree(FIXTURES, run_directory)
    return run_directory


def _write_approval(path: Path, rows: list[tuple[object, object, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(APPROVAL_HEADER)
        writer.writerows(rows)


def _scan_run_rows(
    scan_id: UUID,
    specs: Sequence[tuple[str, str]],
) -> tuple[tuple[LibraryScanRow, ...], tuple[ScanErrorRow, ...]]:
    """Build validated schema 1 inventory/error rows without any real scan.

    ``specs`` is a sequence of ``(path, record_status)`` pairs. A
    ``record_status`` of ``"error"`` gets a linked ``scan_errors.csv`` finding
    so the resulting rows satisfy schema 1's cross-artifact contract.
    """
    # A single shared size/mtime/fingerprint keeps generation near-linear: the
    # per-row cost that cannot be hoisted out of the loop is exactly the two
    # UUIDv5 derivations every row must have anyway (record id keyed by path).
    size = 123_456
    modified_time_ns = 1_700_000_000_123_456_789
    fingerprint = make_file_fingerprint(size, modified_time_ns)

    library_rows: list[LibraryScanRow] = []
    error_rows: list[ScanErrorRow] = []
    for index, (path, record_status) in enumerate(specs):
        file_record_id = make_file_record_id(scan_id, path)
        row = LibraryScanRow(
            scan_id=scan_id,
            file_record_id=file_record_id,
            file_fingerprint=fingerprint,
            path=path,
            extension=".flac",
            file_type="audio",
            file_size_bytes=size,
            modified_time_ns=modified_time_ns,
            artist="Synthetic Artist",
            album_artist="Synthetic Album Artist",
            title=f"Synthetic Track {index}",
            album="Synthetic Album",
            date="2024-01-02",
            release_year=2024,
            track_number=index + 1,
            track_total=len(specs),
            disc_number=1,
            disc_total=1,
            genre="Rock",
            composer="",
            is_compilation=False,
            codec="FLAC",
            container="flac",
            bitrate_kbps=Decimal("900.5"),
            duration_seconds=Decimal("201.25"),
            sample_rate_hz=44100,
            bit_depth=16,
            channels=2,
            record_status=record_status,
        )
        # Fixture rows are validated for real inside create_staging_plan's own
        # validate_artifact_set call; round-tripping here too would just
        # double the cost of building a 100,000-row fixture.
        library_rows.append(row)
        if record_status == "error":
            error_row = ScanErrorRow(
                scan_id=scan_id,
                file_record_id=file_record_id,
                path=path,
                stage="metadata",
                severity="error",
                error_code="metadata_read_failed",
                message="synthetic metadata read failure",
            )
            error_rows.append(ScanErrorRow.from_csv_row(error_row.to_csv_row()))
    return tuple(library_rows), tuple(error_rows)


def _write_scan_run(
    run_directory: Path,
    scan_id: UUID,
    library_rows: Sequence[LibraryScanRow],
    error_rows: Sequence[ScanErrorRow],
) -> None:
    """Write a complete schema 1 scan run directory without a real scan.

    Used only to build fixtures large or specific enough (record_status
    "error" rows, 100,000-row scale) that copying the committed golden
    fixture would not exercise; production code always reaches
    ``scan_manifest.json`` through the real scanner or through
    ``create_scan_run``.
    """
    run_directory.mkdir(parents=True)
    library_path = run_directory / "library_scan.csv"
    with library_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=LIBRARY_SCAN_HEADER, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(row.to_csv_row() for row in library_rows)
    errors_path = run_directory / "scan_errors.csv"
    with errors_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=SCAN_ERRORS_HEADER, lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(row.to_csv_row() for row in error_rows)

    error_count = sum(1 for row in error_rows if row.severity == "error")
    fatal_count = sum(1 for row in error_rows if row.severity == "fatal")
    info_count = sum(1 for row in error_rows if row.severity == "info")
    state = "incomplete" if error_count or fatal_count else "complete"
    manifest = {
        "schema_version": "1.0.0",
        "application_version": "0.5.0",
        "scan_id": str(scan_id),
        "state": state,
        "started_at": "2026-07-11T20:00:00Z",
        "completed_at": "2026-07-11T20:00:01Z",
        "artifacts": {
            "library_scan": {
                "filename": "library_scan.csv",
                "role": "primary",
                "application_version": "0.5.0",
                "generated_at": "2026-07-11T20:00:01Z",
                "row_count": len(library_rows),
                "sha256": hashlib.sha256(library_path.read_bytes()).hexdigest(),
            },
            "scan_errors": {
                "filename": "scan_errors.csv",
                "role": "primary",
                "application_version": "0.5.0",
                "generated_at": "2026-07-11T20:00:01Z",
                "row_count": len(error_rows),
                "sha256": hashlib.sha256(errors_path.read_bytes()).hexdigest(),
            },
        },
        "counts": {
            "inventory_rows": len(library_rows),
            "info_findings": info_count,
            "error_findings": error_count,
            "fatal_findings": fatal_count,
            "skipped_symlinks": 0,
        },
        "configuration": {
            "ignore": [],
            "path_mode": "relative",
            "follow_symlinks": False,
        },
    }
    (run_directory / "scan_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


class StagingPlanTests(unittest.TestCase):
    def test_registers_sorted_plan_and_upgrades_schema_without_source_access(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "private-approval.csv"
            _write_approval(
                approval,
                [
                    (SCAN_ID, ARCHIVE_ID, "stage"),
                    (SCAN_ID, AUDIO_ID, "stage"),
                ],
            )
            source = root / "source-must-not-be-read"
            staging = root / "staging-must-not-exist"
            source.write_bytes(b"untouched source bytes")
            before = source.read_bytes()

            outcome = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )

            self.assertEqual(outcome.manifest.schema_version, STAGING_SCHEMA_VERSION)
            self.assertEqual(outcome.stage_id, STAGE_ID)
            self.assertEqual(
                [row.source_path for row in outcome.rows],
                ["Artist/Album/01 Track.flac", "Incoming/Archive.zip"],
            )
            self.assertEqual(outcome.rows[0].plan_status, "planned")
            self.assertEqual(outcome.rows[0].reason_code, "")
            self.assertEqual(outcome.rows[1].plan_status, "not_eligible")
            self.assertEqual(outcome.rows[1].reason_code, "not_audio")
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(staging.exists())
            self.assertNotIn(str(approval), json.dumps(outcome.manifest.to_dict()))
            validate_artifact_set(run_directory / "scan_manifest.json")

    def test_skip_rows_are_validated_but_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "approval.csv"
            _write_approval(
                approval,
                [(SCAN_ID, ARCHIVE_ID, "skip"), (SCAN_ID, AUDIO_ID, "stage")],
            )
            outcome = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )
            self.assertEqual(len(outcome.rows), 1)
            self.assertEqual(outcome.rows[0].file_record_id, AUDIO_ID)

    def test_replanning_replaces_only_plan_and_preserves_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            analyzed = analyze_scan_run(run_directory)
            existing_entries = dict(analyzed.manifest.artifacts)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            first = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )
            second_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
            second = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: second_id,
                clock=_clock,
            )
            self.assertNotEqual(first.stage_id, second.stage_id)
            self.assertEqual(second.rows[0].stage_id, second_id)
            for name, entry in existing_entries.items():
                self.assertEqual(second.manifest.artifacts[name], entry)

    def test_registration_failure_restores_existing_plan_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )
            manifest_path = run_directory / "scan_manifest.json"
            plan_path = run_directory / "staging_plan.csv"
            original_manifest = manifest_path.read_bytes()
            original_plan = plan_path.read_bytes()
            real_replace = __import__("os").replace

            def fail_manifest(source: object, target: object) -> None:
                if Path(target) == manifest_path:
                    raise OSError("injected manifest failure")
                real_replace(source, target)

            with (
                mock.patch("music_manager.staging_plans.os.replace", fail_manifest),
                self.assertRaisesRegex(OSError, "injected manifest"),
            ):
                create_staging_plan(
                    run_directory,
                    approval,
                    stage_id_factory=lambda: OTHER_SCAN_ID,
                    clock=_clock,
                )
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(plan_path.read_bytes(), original_plan)
            self.assertFalse(any(run_directory.glob(".*.tmp")))

    def test_invalid_approval_inputs_register_nothing(self) -> None:
        cases = {
            "wrong header": "file_record_id,scan_id,decision\n",
            "unknown decision": f"{','.join(APPROVAL_HEADER)}\n{SCAN_ID},{AUDIO_ID},copy\n",
            "mixed scan": f"{','.join(APPROVAL_HEADER)}\n{OTHER_SCAN_ID},{AUDIO_ID},stage\n",
            "unknown id": (
                f"{','.join(APPROVAL_HEADER)}\n{SCAN_ID},"
                f"{uuid5(SCAN_ID, 'unknown')},stage\n"
            ),
            "empty selection": f"{','.join(APPROVAL_HEADER)}\n{SCAN_ID},{AUDIO_ID},skip\n",
            "malformed": f"{','.join(APPROVAL_HEADER)}\n{SCAN_ID},{AUDIO_ID},stage,extra\n",
            "invalid utf8": b"\xff\xfe",
        }
        for name, payload in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_directory = _copy_run(root)
                approval = root / "approval.csv"
                if isinstance(payload, bytes):
                    approval.write_bytes(payload)
                else:
                    approval.write_text(payload, encoding="utf-8")
                original = (run_directory / "scan_manifest.json").read_bytes()
                with self.assertRaises(ArtifactValidationError):
                    create_staging_plan(run_directory, approval)
                self.assertEqual(
                    (run_directory / "scan_manifest.json").read_bytes(), original
                )
                self.assertFalse((run_directory / "staging_plan.csv").exists())

    def test_duplicate_approval_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "approval.csv"
            _write_approval(
                path,
                [(SCAN_ID, AUDIO_ID, "stage"), (SCAN_ID, AUDIO_ID, "skip")],
            )
            with self.assertRaisesRegex(ArtifactValidationError, "duplicate"):
                load_approval_file(path, SCAN_ID)

    def test_non_directory_run_path_is_rejected(self) -> None:
        # A scan run selector must be a versioned run directory. A path to a
        # bare CSV file (for example, a pre-schema-1 flat report) is not a
        # scan run and must be rejected before any approval-driven work.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            with self.assertRaises(ArtifactValidationError):
                create_staging_plan(FIXTURES / "library_scan.csv", approval)

    def test_legacy_schema_version_is_rejected_at_the_version_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "0.2.0"
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            with self.assertRaisesRegex(
                ArtifactValidationError, "unsupported schema major"
            ):
                create_staging_plan(run_directory, approval)

    def test_end_to_end_plan_registers_one_hundred_thousand_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan_id = UUID("70707070-7070-4707-8707-707070707070")
            run_directory = root / str(scan_id)
            paths = [f"Artist/Album/{index:06d} Track.flac" for index in range(100_000)]
            library_rows, error_rows = _scan_run_rows(
                scan_id, [(path, "ok") for path in paths]
            )
            _write_scan_run(run_directory, scan_id, library_rows, error_rows)
            approval = root / "approval.csv"
            with approval.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(APPROVAL_HEADER)
                for row in library_rows:
                    writer.writerow((scan_id, row.file_record_id, "stage"))

            outcome = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )

            self.assertEqual(len(outcome.rows), 100_000)
            self.assertEqual(outcome.rows[0].source_path, paths[0])
            self.assertEqual(outcome.rows[0].plan_status, "planned")
            self.assertEqual(outcome.rows[-1].source_path, paths[-1])
            self.assertEqual(outcome.rows[-1].plan_status, "planned")
            validate_artifact_set(run_directory / "scan_manifest.json")

    def test_invalid_stage_id_or_naive_clock_registers_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            with self.assertRaisesRegex(ValueError, "UUIDv4"):
                create_staging_plan(
                    run_directory,
                    approval,
                    stage_id_factory=lambda: AUDIO_ID,
                )
            with self.assertRaisesRegex(ValueError, "timezone-aware"):
                create_staging_plan(
                    run_directory,
                    approval,
                    stage_id_factory=lambda: STAGE_ID,
                    clock=lambda: datetime(2026, 7, 11),
                )
            self.assertNotIn(
                "staging_plan",
                load_scan_manifest(run_directory / "scan_manifest.json").artifacts,
            )

    def test_approval_path_errors_surface_as_artifact_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "does-not-exist.csv"
            with self.assertRaises(ArtifactValidationError):
                load_approval_file(missing, SCAN_ID)

            directory_path = root / "a-directory.csv"
            directory_path.mkdir()
            with self.assertRaises(ArtifactValidationError):
                load_approval_file(directory_path, SCAN_ID)

    def test_record_not_ok_row_is_not_eligible_with_reason_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan_id = UUID("10101010-1010-4101-8101-101010101010")
            run_directory = root / str(scan_id)
            library_rows, error_rows = _scan_run_rows(
                scan_id,
                [("Artist/Album/01 Broken.flac", "error")],
            )
            _write_scan_run(run_directory, scan_id, library_rows, error_rows)
            approval = root / "approval.csv"
            _write_approval(
                approval, [(scan_id, library_rows[0].file_record_id, "stage")]
            )

            outcome = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )

            self.assertEqual(len(outcome.rows), 1)
            self.assertEqual(outcome.rows[0].plan_status, "not_eligible")
            self.assertEqual(outcome.rows[0].reason_code, "record_not_ok")
            validate_artifact_set(run_directory / "scan_manifest.json")

    def test_replan_deregisters_previously_applied_copy_and_error_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scan_id = UUID("20202020-2020-4202-8202-202020202020")
            run_directory = root / str(scan_id)
            library_rows, error_rows = _scan_run_rows(
                scan_id,
                [
                    ("Artist/Album/01 Track.flac", "ok"),
                    ("Artist/Album/02 Track.flac", "ok"),
                ],
            )
            _write_scan_run(run_directory, scan_id, library_rows, error_rows)
            approval = root / "approval.csv"
            _write_approval(
                approval,
                [
                    (scan_id, library_rows[0].file_record_id, "stage"),
                    (scan_id, library_rows[1].file_record_id, "stage"),
                ],
            )
            first_stage_id = UUID("30303030-3030-4303-8303-303030303030")
            create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: first_stage_id,
                clock=_clock,
            )

            # Hand-register a valid staging_copies/staging_errors pair, the
            # way a completed `stage apply` would (not yet implemented as of
            # issue #54), building rows the way test_staging_schema.py does.
            copy_row = StagingCopyRow(
                scan_id=scan_id,
                stage_id=first_stage_id,
                file_record_id=library_rows[0].file_record_id,
                source_path=library_rows[0].path,
                stage_relative_path=f"files/{library_rows[0].path}",
                source_size_bytes=library_rows[0].file_size_bytes,
                source_sha256="a" * 64,
                staged_size_bytes=library_rows[0].file_size_bytes,
                staged_sha256="a" * 64,
                copy_status="verified",
            )
            error_row = StagingErrorRow(
                scan_id=scan_id,
                stage_id=first_stage_id,
                file_record_id=library_rows[1].file_record_id,
                source_path=library_rows[1].path,
                stage="verification",
                error_code="digest_mismatch",
                message="synthetic verification failure",
            )
            copies_path = run_directory / "staging_copies.csv"
            with copies_path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=STAGING_COPIES_HEADER,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(copy_row.to_csv_row())
            errors_path = run_directory / "staging_errors.csv"
            with errors_path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(
                    output,
                    fieldnames=STAGING_ERRORS_HEADER,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerow(error_row.to_csv_row())

            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["staging_copies"] = {
                "filename": "staging_copies.csv",
                "role": "derived",
                "application_version": "0.5.0",
                "generated_at": "2026-07-11T20:00:02Z",
                "row_count": 1,
                "sha256": hashlib.sha256(copies_path.read_bytes()).hexdigest(),
                "configuration": {},
            }
            manifest["artifacts"]["staging_errors"] = {
                "filename": "staging_errors.csv",
                "role": "derived",
                "application_version": "0.5.0",
                "generated_at": "2026-07-11T20:00:02Z",
                "row_count": 1,
                "sha256": hashlib.sha256(errors_path.read_bytes()).hexdigest(),
                "configuration": {},
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            # Confirm the hand-registered fixture is valid before the replan.
            validate_artifact_set(manifest_path)

            second_stage_id = UUID("40404040-4040-4404-8404-404040404040")
            outcome = create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: second_stage_id,
                clock=_clock,
            )

            self.assertNotIn("staging_copies", outcome.manifest.artifacts)
            self.assertNotIn("staging_errors", outcome.manifest.artifacts)
            self.assertIn("staging_plan", outcome.manifest.artifacts)
            validate_artifact_set(manifest_path)

    def test_plan_csv_bytes_are_deterministic_modulo_stage_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first_run = _copy_run(first_root)
            second_run = _copy_run(second_root)
            first_approval = first_root / "approval.csv"
            second_approval = second_root / "approval.csv"
            rows = [(SCAN_ID, ARCHIVE_ID, "stage"), (SCAN_ID, AUDIO_ID, "stage")]
            _write_approval(first_approval, rows)
            _write_approval(second_approval, rows)
            first_stage_id = UUID("50505050-5050-4505-8505-505050505050")
            second_stage_id = UUID("60606060-6060-4606-8606-606060606060")

            create_staging_plan(
                first_run,
                first_approval,
                stage_id_factory=lambda: first_stage_id,
                clock=_clock,
            )
            create_staging_plan(
                second_run,
                second_approval,
                stage_id_factory=lambda: second_stage_id,
                clock=_clock,
            )

            first_bytes = (first_run / "staging_plan.csv").read_bytes()
            second_bytes = (second_run / "staging_plan.csv").read_bytes()
            self.assertNotEqual(first_bytes, second_bytes)
            normalized_first = first_bytes.replace(
                str(first_stage_id).encode("ascii"), b"STAGE_ID"
            )
            normalized_second = second_bytes.replace(
                str(second_stage_id).encode("ascii"), b"STAGE_ID"
            )
            self.assertEqual(normalized_first, normalized_second)

    def test_rollback_double_failure_preserves_backup_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )
            manifest_path = run_directory / "scan_manifest.json"
            plan_path = run_directory / "staging_plan.csv"
            original_plan_bytes = plan_path.read_bytes()
            real_replace = __import__("os").replace
            manifest_writes = 0
            final_path_writes = 0

            def fail_manifest_and_backup_restore(
                source: object, target: object
            ) -> None:
                nonlocal manifest_writes, final_path_writes
                target_path = Path(target)
                if target_path == manifest_path:
                    manifest_writes += 1
                    if manifest_writes >= 2:
                        raise OSError("injected manifest failure")
                if target_path == plan_path:
                    final_path_writes += 1
                    if final_path_writes >= 2:
                        raise OSError("injected backup restore failure")
                real_replace(source, target)

            with (
                mock.patch(
                    "music_manager.staging_plans.os.replace",
                    fail_manifest_and_backup_restore,
                ),
                self.assertRaisesRegex(OSError, "injected manifest failure") as caught,
            ):
                create_staging_plan(
                    run_directory,
                    approval,
                    stage_id_factory=lambda: UUID(
                        "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
                    ),
                    clock=_clock,
                )

            notes = getattr(caught.exception, "__notes__", ())
            self.assertTrue(any("rollback also failed" in note for note in notes))
            backups = list(run_directory.glob(".*.tmp"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original_plan_bytes)

    def test_crash_between_phase_one_and_csv_swap_is_self_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )
            manifest_path = run_directory / "scan_manifest.json"
            real_replace = __import__("os").replace
            calls = 0

            def fail_after_first_call(source: object, target: object) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    real_replace(source, target)
                    return
                raise OSError("injected crash between phases")

            with (
                mock.patch(
                    "music_manager.staging_plans.os.replace",
                    fail_after_first_call,
                ),
                self.assertRaisesRegex(OSError, "injected crash between phases"),
            ):
                create_staging_plan(
                    run_directory,
                    approval,
                    stage_id_factory=lambda: UUID(
                        "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
                    ),
                    clock=_clock,
                )

            self.assertGreaterEqual(calls, 1)
            manifest = load_scan_manifest(manifest_path)
            self.assertNotIn("staging_plan", manifest.artifacts)
            validate_artifact_set(manifest_path)

    def test_concurrent_manifest_mutation_is_detected_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_directory = _copy_run(root)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            create_staging_plan(
                run_directory,
                approval,
                stage_id_factory=lambda: STAGE_ID,
                clock=_clock,
            )
            manifest_path = run_directory / "scan_manifest.json"
            plan_path = run_directory / "staging_plan.csv"
            original_plan_bytes = plan_path.read_bytes()
            real_replace = __import__("os").replace
            interloper_bytes = b""
            mutated = False

            def mutate_on_swap(source: object, target: object) -> None:
                nonlocal interloper_bytes, mutated
                real_replace(source, target)
                if not mutated and Path(target) == plan_path:
                    mutated = True
                    data = json.loads(manifest_path.read_text(encoding="utf-8"))
                    data["application_version"] = "9.9.9-interloper"
                    interloper_bytes = (json.dumps(data, indent=2) + "\n").encode(
                        "utf-8"
                    )
                    manifest_path.write_bytes(interloper_bytes)

            with (
                mock.patch(
                    "music_manager.staging_plans.os.replace",
                    mutate_on_swap,
                ),
                self.assertRaisesRegex(
                    ArtifactValidationError,
                    "scan manifest changed during staging plan registration",
                ),
            ):
                create_staging_plan(
                    run_directory,
                    approval,
                    stage_id_factory=lambda: UUID(
                        "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
                    ),
                    clock=_clock,
                )

            self.assertEqual(manifest_path.read_bytes(), interloper_bytes)
            self.assertEqual(plan_path.read_bytes(), original_plan_bytes)
            self.assertFalse(any(run_directory.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
