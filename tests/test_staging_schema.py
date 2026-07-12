"""Schema 1.2 staging artifact model and validator tests."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from music_manager.artifact_schema import (
    MATCHING_SCHEMA_VERSION,
    SCHEMA_VERSION,
    STAGING_APPLY_ARTIFACT_NAMES,
    STAGING_ARTIFACT_NAMES,
    STAGING_COPIES_HEADER,
    STAGING_ERRORS_HEADER,
    STAGING_PLAN_HEADER,
    STAGING_SCHEMA_VERSION,
    ArtifactValidationError,
    ScanManifest,
    StagingCopyRow,
    StagingErrorRow,
    StagingPlanRow,
    load_staging_copies,
    load_staging_errors,
    load_staging_plan,
    validate_artifact_set,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
STAGE_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
FILE_RECORD_ID = UUID("aec6a2b3-b8d7-55ea-a953-25d4c1a793dd")
FILE_RECORD_ID_B = UUID("b1c2d3e4-f5a6-5789-8bcd-ef0123456789")
DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DIGEST_B = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def _plan_row(
    *,
    source_path: str = "artist/album/track.flac",
    file_record_id: UUID = FILE_RECORD_ID,
    plan_status: str = "planned",
    reason_code: str = "",
) -> dict[str, str]:
    return {
        "scan_id": str(SCAN_ID),
        "stage_id": str(STAGE_ID),
        "file_record_id": str(file_record_id),
        "source_path": source_path,
        "stage_relative_path": f"files/{source_path}",
        "plan_status": plan_status,
        "reason_code": reason_code,
    }


def _copy_row(
    *,
    source_path: str = "artist/album/track.flac",
    file_record_id: UUID = FILE_RECORD_ID,
    digest: str = DIGEST,
    size: str = "1024",
) -> dict[str, str]:
    return {
        "scan_id": str(SCAN_ID),
        "stage_id": str(STAGE_ID),
        "file_record_id": str(file_record_id),
        "source_path": source_path,
        "stage_relative_path": f"files/{source_path}",
        "source_size_bytes": size,
        "source_sha256": digest,
        "staged_size_bytes": size,
        "staged_sha256": digest,
        "copy_status": "verified",
    }


def _error_row(
    *,
    source_path: str = "artist/album/track.flac",
    file_record_id: UUID = FILE_RECORD_ID,
    stage: str = "source_preflight",
    error_code: str = "source_missing",
) -> dict[str, str]:
    return {
        "scan_id": str(SCAN_ID),
        "stage_id": str(STAGE_ID),
        "file_record_id": str(file_record_id),
        "source_path": source_path,
        "stage": stage,
        "error_code": error_code,
        "message": "synthetic staging error",
    }


def _write_csv(
    path: Path,
    header: tuple[str, ...],
    rows: list[dict[str, str]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as report:
        writer = csv.DictWriter(
            report,
            fieldnames=header,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def _add_artifact(
    manifest: dict,
    directory: Path,
    logical_name: str,
    filename: str,
    row_count: int,
) -> None:
    payload = (directory / filename).read_bytes()
    manifest["artifacts"][logical_name] = {
        "filename": filename,
        "role": "derived",
        "application_version": "0.5.0",
        "generated_at": "2026-07-11T12:00:00Z",
        "row_count": row_count,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "configuration": {},
    }


class StagingSchemaModelTests(unittest.TestCase):
    def test_staging_adds_reader_support_without_changing_scan_output_version(
        self,
    ) -> None:
        self.assertEqual(SCHEMA_VERSION, "1.0.0")
        self.assertEqual(MATCHING_SCHEMA_VERSION, "1.1.0")
        self.assertEqual(STAGING_SCHEMA_VERSION, "1.2.0")

    def test_exact_ordered_headers_match_the_contract(self) -> None:
        self.assertEqual(
            STAGING_PLAN_HEADER,
            (
                "scan_id",
                "stage_id",
                "file_record_id",
                "source_path",
                "stage_relative_path",
                "plan_status",
                "reason_code",
            ),
        )
        self.assertEqual(
            STAGING_COPIES_HEADER,
            (
                "scan_id",
                "stage_id",
                "file_record_id",
                "source_path",
                "stage_relative_path",
                "source_size_bytes",
                "source_sha256",
                "staged_size_bytes",
                "staged_sha256",
                "copy_status",
            ),
        )
        self.assertEqual(
            STAGING_ERRORS_HEADER,
            (
                "scan_id",
                "stage_id",
                "file_record_id",
                "source_path",
                "stage",
                "error_code",
                "message",
            ),
        )

    def test_rows_round_trip_without_coercion(self) -> None:
        cases = (
            (
                StagingPlanRow,
                _plan_row(plan_status="not_eligible", reason_code="archive_row"),
            ),
            (StagingCopyRow, _copy_row()),
            (StagingErrorRow, _error_row()),
        )
        for model, source in cases:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.from_csv_row(source).to_csv_row(), source)

    def test_plan_rows_reject_invalid_domains_and_paths(self) -> None:
        cases = {
            "bad plan status": ("plan_status", "approved"),
            "planned with reason": ("reason_code", "should_be_empty"),
            "absolute source path": ("source_path", "/tmp/track.flac"),
            "stage path without files prefix": (
                "stage_relative_path",
                "artist/album/track.flac",
            ),
            "windows absolute path": ("source_path", r"C:\music\track.flac"),
            "dotdot segment": ("source_path", "artist/../track.flac"),
            "backslash separators": ("source_path", r"artist\album\track.flac"),
        }
        for label, (field, replacement) in cases.items():
            with self.subTest(label=label):
                row = _plan_row()
                row[field] = replacement
                with self.assertRaises(ArtifactValidationError):
                    StagingPlanRow.from_csv_row(row)

        not_eligible = _plan_row(plan_status="not_eligible", reason_code="")
        with self.assertRaisesRegex(ArtifactValidationError, "reason_code"):
            StagingPlanRow.from_csv_row(not_eligible)

        not_eligible_bad_reason = _plan_row(
            plan_status="not_eligible",
            reason_code="Not-Eligible",
        )
        with self.assertRaisesRegex(ArtifactValidationError, "snake case"):
            StagingPlanRow.from_csv_row(not_eligible_bad_reason)

    def test_copy_rows_reject_invalid_domains(self) -> None:
        cases = {
            "unknown status": ("copy_status", "copied"),
            "size mismatch": ("staged_size_bytes", "2048"),
            "digest mismatch": ("staged_sha256", DIGEST_B),
            "uppercase digest": ("source_sha256", DIGEST.upper()),
            "negative size": ("source_size_bytes", "-1"),
        }
        for label, (field, replacement) in cases.items():
            with self.subTest(label=label):
                row = _copy_row()
                row[field] = replacement
                with self.assertRaises(ArtifactValidationError):
                    StagingCopyRow.from_csv_row(row)

    def test_error_rows_reject_invalid_domains(self) -> None:
        cases = {
            "unknown stage": ("stage", "planning"),
            "uppercase error code": ("error_code", "SourceMissing"),
            "empty message": ("message", ""),
            "empty error code": ("error_code", ""),
        }
        for label, (field, replacement) in cases.items():
            with self.subTest(label=label):
                row = _error_row()
                row[field] = replacement
                with self.assertRaises(ArtifactValidationError):
                    StagingErrorRow.from_csv_row(row)

    def test_loaders_reject_reordered_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "staging_plan.csv"
            path.write_text(
                "stage_id,scan_id,file_record_id,source_path,"
                "stage_relative_path,plan_status,reason_code\n"
                f"{STAGE_ID},{SCAN_ID},{FILE_RECORD_ID},"
                "artist/album/track.flac,files/artist/album/track.flac,planned,\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "required order",
            ):
                load_staging_plan(path)

    def test_loaders_enforce_required_sort_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)

            plan_path = directory / "staging_plan.csv"
            _write_csv(
                plan_path,
                STAGING_PLAN_HEADER,
                [
                    _plan_row(
                        source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B
                    ),
                    _plan_row(source_path="a/track.flac"),
                ],
            )
            with self.assertRaisesRegex(ArtifactValidationError, "sort order"):
                load_staging_plan(plan_path)

            copy_path = directory / "staging_copies.csv"
            _write_csv(
                copy_path,
                STAGING_COPIES_HEADER,
                [
                    _copy_row(
                        source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B
                    ),
                    _copy_row(source_path="a/track.flac"),
                ],
            )
            with self.assertRaisesRegex(ArtifactValidationError, "sort order"):
                load_staging_copies(copy_path)

            error_path = directory / "staging_errors.csv"
            _write_csv(
                error_path,
                STAGING_ERRORS_HEADER,
                [
                    _error_row(
                        source_path="a/track.flac",
                        stage="verification",
                        error_code="digest_mismatch",
                    ),
                    _error_row(
                        source_path="a/track.flac",
                        stage="copy",
                        error_code="write_failed",
                    ),
                ],
            )
            with self.assertRaisesRegex(ArtifactValidationError, "sort order"):
                load_staging_errors(error_path)

    def test_public_loaders_accept_header_only_reports(self) -> None:
        cases = (
            (load_staging_plan, STAGING_PLAN_HEADER),
            (load_staging_copies, STAGING_COPIES_HEADER),
            (load_staging_errors, STAGING_ERRORS_HEADER),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, (loader, header) in enumerate(cases):
                with self.subTest(loader=loader.__name__):
                    path = directory / f"{index}.csv"
                    path.write_text(f"{','.join(header)}\n", encoding="utf-8")
                    self.assertEqual(loader(path), ())


class StagingArtifactSetTests(unittest.TestCase):
    def test_schema_1_2_plan_only_family_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(SCAN_ID)
            shutil.copytree(FIXTURES, run_directory)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = STAGING_SCHEMA_VERSION

            rows = [
                _plan_row(source_path="a/track.flac"),
                _plan_row(
                    source_path="b/track.flac",
                    file_record_id=FILE_RECORD_ID_B,
                    plan_status="not_eligible",
                    reason_code="archive_row",
                ),
            ]
            _write_csv(
                run_directory / "staging_plan.csv",
                STAGING_PLAN_HEADER,
                rows,
            )
            _add_artifact(
                manifest,
                run_directory,
                "staging_plan",
                "staging_plan.csv",
                len(rows),
            )
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )

            artifacts = validate_artifact_set(manifest_path)
            self.assertEqual(len(artifacts.staging_plan_rows), 2)
            self.assertEqual(artifacts.staging_copy_rows, ())
            self.assertEqual(artifacts.staging_error_rows, ())

    def test_schema_1_2_full_staging_family_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(SCAN_ID)
            shutil.copytree(FIXTURES, run_directory)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = STAGING_SCHEMA_VERSION

            plan_rows = [_plan_row(source_path="a/track.flac")]
            copy_rows = [_copy_row(source_path="a/track.flac")]
            error_rows = [
                _error_row(
                    source_path="b/track.flac",
                    file_record_id=FILE_RECORD_ID_B,
                )
            ]
            specs = (
                (
                    "staging_plan",
                    "staging_plan.csv",
                    STAGING_PLAN_HEADER,
                    plan_rows,
                ),
                (
                    "staging_copies",
                    "staging_copies.csv",
                    STAGING_COPIES_HEADER,
                    copy_rows,
                ),
                (
                    "staging_errors",
                    "staging_errors.csv",
                    STAGING_ERRORS_HEADER,
                    error_rows,
                ),
            )
            for logical_name, filename, header, rows in specs:
                _write_csv(run_directory / filename, header, rows)
                _add_artifact(
                    manifest,
                    run_directory,
                    logical_name,
                    filename,
                    len(rows),
                )
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )

            artifacts = validate_artifact_set(manifest_path)
            self.assertEqual(len(artifacts.staging_plan_rows), 1)
            self.assertEqual(len(artifacts.staging_copy_rows), 1)
            self.assertEqual(len(artifacts.staging_error_rows), 1)

    def test_schema_1_0_and_1_1_reject_staging_artifact_names(self) -> None:
        for schema_version in (SCHEMA_VERSION, MATCHING_SCHEMA_VERSION):
            with self.subTest(schema_version=schema_version):
                manifest = json.loads(
                    (FIXTURES / "scan_manifest.json").read_text(encoding="utf-8")
                )
                manifest["schema_version"] = schema_version
                for logical_name in STAGING_ARTIFACT_NAMES:
                    manifest["artifacts"][logical_name] = {
                        "filename": f"{logical_name}.csv",
                        "role": "derived",
                        "application_version": "0.5.0",
                        "generated_at": "2026-07-11T12:00:00Z",
                        "row_count": 0,
                        "sha256": "0" * 64,
                        "configuration": {},
                    }
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    "requires schema 1.2",
                ):
                    ScanManifest.from_dict(manifest)

    def test_schema_1_2_requires_complete_apply_family(self) -> None:
        manifest = json.loads(
            (FIXTURES / "scan_manifest.json").read_text(encoding="utf-8")
        )
        manifest["schema_version"] = STAGING_SCHEMA_VERSION
        manifest["artifacts"]["staging_plan"] = {
            "filename": "staging_plan.csv",
            "role": "derived",
            "application_version": "0.5.0",
            "generated_at": "2026-07-11T12:00:00Z",
            "row_count": 0,
            "sha256": "0" * 64,
            "configuration": {},
        }
        for logical_name in STAGING_APPLY_ARTIFACT_NAMES:
            incomplete = json.loads(json.dumps(manifest))
            incomplete["artifacts"][logical_name] = {
                "filename": f"{logical_name}.csv",
                "role": "derived",
                "application_version": "0.5.0",
                "generated_at": "2026-07-11T12:00:00Z",
                "row_count": 0,
                "sha256": "0" * 64,
                "configuration": {},
            }
            with self.subTest(present=logical_name):
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    "staging apply artifact family is missing",
                ):
                    ScanManifest.from_dict(incomplete)

    def test_schema_1_2_apply_family_requires_plan(self) -> None:
        manifest = json.loads(
            (FIXTURES / "scan_manifest.json").read_text(encoding="utf-8")
        )
        manifest["schema_version"] = STAGING_SCHEMA_VERSION
        for logical_name in STAGING_APPLY_ARTIFACT_NAMES:
            manifest["artifacts"][logical_name] = {
                "filename": f"{logical_name}.csv",
                "role": "derived",
                "application_version": "0.5.0",
                "generated_at": "2026-07-11T12:00:00Z",
                "row_count": 0,
                "sha256": "0" * 64,
                "configuration": {},
            }
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "require staging_plan",
        ):
            ScanManifest.from_dict(manifest)

    def test_existing_schema_1_0_runs_remain_valid(self) -> None:
        artifacts = validate_artifact_set(FIXTURES / "scan_manifest.json")
        self.assertEqual(artifacts.manifest.schema_version, SCHEMA_VERSION)
        self.assertEqual(artifacts.staging_plan_rows, ())
        self.assertEqual(artifacts.staging_copy_rows, ())
        self.assertEqual(artifacts.staging_error_rows, ())


if __name__ == "__main__":
    unittest.main()
