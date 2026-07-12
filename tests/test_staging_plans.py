"""Tests for approval parsing and plan-only staging registration."""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid5

from music_manager.analysis_runs import analyze_scan_run
from music_manager.artifact_schema import (
    STAGING_SCHEMA_VERSION,
    ArtifactValidationError,
    load_scan_manifest,
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

    def test_legacy_csv_is_not_a_scan_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            approval = root / "approval.csv"
            _write_approval(approval, [(SCAN_ID, AUDIO_ID, "stage")])
            with self.assertRaises(ArtifactValidationError):
                create_staging_plan(FIXTURES / "library_scan.csv", approval)

    def test_parses_one_hundred_thousand_rows_with_constant_time_lookup_sets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "approval.csv"
            namespace = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.writer(output)
                writer.writerow(APPROVAL_HEADER)
                for index in range(100_000):
                    writer.writerow((SCAN_ID, uuid5(namespace, f"row-{index}"), "skip"))
            rows = load_approval_file(path, SCAN_ID)
            self.assertEqual(len(rows), 100_000)
            self.assertEqual(len({row.file_record_id for row in rows}), 100_000)

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


if __name__ == "__main__":
    unittest.main()
