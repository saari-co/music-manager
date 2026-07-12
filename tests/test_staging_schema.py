"""Schema 1.2 staging artifact model and validator tests."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid5

from music_manager.artifact_schema import (
    MATCHING_SCHEMA_VERSION,
    MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
    MUSICBRAINZ_ALBUM_GROUPS_HEADER,
    MUSICBRAINZ_MATCH_RESULTS_HEADER,
    MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
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
    make_file_record_id,
    validate_artifact_set,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
OTHER_SCAN_ID = UUID("99999999-9999-4999-8999-999999999999")
STAGE_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
OTHER_STAGE_ID = UUID("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")
DEFAULT_SOURCE_PATH = "artist/album/track.flac"
FILE_RECORD_ID = make_file_record_id(SCAN_ID, DEFAULT_SOURCE_PATH)
FILE_RECORD_ID_B = make_file_record_id(SCAN_ID, "b/track.flac")
LIBRARY_FILE_RECORD_ID = UUID("aec6a2b3-b8d7-55ea-a953-25d4c1a793dd")
DIGEST = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
DIGEST_B = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"


def _plan_row(
    *,
    scan_id: UUID = SCAN_ID,
    stage_id: UUID = STAGE_ID,
    source_path: str = "artist/album/track.flac",
    file_record_id: UUID | None = None,
    stage_relative_path: str | None = None,
    plan_status: str = "planned",
    reason_code: str = "",
) -> dict[str, str]:
    resolved_record_id = (
        file_record_id
        if file_record_id is not None
        else make_file_record_id(scan_id, source_path)
    )
    return {
        "scan_id": str(scan_id),
        "stage_id": str(stage_id),
        "file_record_id": str(resolved_record_id),
        "source_path": source_path,
        "stage_relative_path": (
            stage_relative_path
            if stage_relative_path is not None
            else f"files/{source_path}"
        ),
        "plan_status": plan_status,
        "reason_code": reason_code,
    }


def _copy_row(
    *,
    scan_id: UUID = SCAN_ID,
    stage_id: UUID = STAGE_ID,
    source_path: str = "artist/album/track.flac",
    file_record_id: UUID | None = None,
    stage_relative_path: str | None = None,
    digest: str = DIGEST,
    size: str = "1024",
) -> dict[str, str]:
    resolved_record_id = (
        file_record_id
        if file_record_id is not None
        else make_file_record_id(scan_id, source_path)
    )
    return {
        "scan_id": str(scan_id),
        "stage_id": str(stage_id),
        "file_record_id": str(resolved_record_id),
        "source_path": source_path,
        "stage_relative_path": (
            stage_relative_path
            if stage_relative_path is not None
            else f"files/{source_path}"
        ),
        "source_size_bytes": size,
        "source_sha256": digest,
        "staged_size_bytes": size,
        "staged_sha256": digest,
        "copy_status": "verified",
    }


def _error_row(
    *,
    scan_id: UUID = SCAN_ID,
    stage_id: UUID = STAGE_ID,
    source_path: str = "artist/album/track.flac",
    file_record_id: UUID | None = None,
    stage: str = "source_preflight",
    error_code: str = "source_missing",
) -> dict[str, str]:
    resolved_record_id = (
        file_record_id
        if file_record_id is not None
        else make_file_record_id(scan_id, source_path)
    )
    return {
        "scan_id": str(scan_id),
        "stage_id": str(stage_id),
        "file_record_id": str(resolved_record_id),
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


def _build_staging_manifest(
    run_directory: Path,
    *,
    plan_rows: list[dict[str, str]] | None = None,
    copy_rows: list[dict[str, str]] | None = None,
    error_rows: list[dict[str, str]] | None = None,
    extra_specs: tuple[
        tuple[str, str, tuple[str, ...], list[dict[str, str]]], ...
    ] = (),
) -> Path:
    """Build a schema 1.2 scan-run directory and return its manifest path.

    Any of ``plan_rows``, ``copy_rows``, or ``error_rows`` left as ``None`` is
    not registered at all; an empty list registers the artifact with zero
    rows. ``extra_specs`` registers additional artifact families (for example
    a MusicBrainz matching family) alongside the staging family.
    """
    shutil.copytree(FIXTURES, run_directory)
    manifest_path = run_directory / "scan_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["schema_version"] = STAGING_SCHEMA_VERSION

    staging_specs = (
        ("staging_plan", "staging_plan.csv", STAGING_PLAN_HEADER, plan_rows),
        ("staging_copies", "staging_copies.csv", STAGING_COPIES_HEADER, copy_rows),
        ("staging_errors", "staging_errors.csv", STAGING_ERRORS_HEADER, error_rows),
    )
    for logical_name, filename, header, rows in (*staging_specs, *extra_specs):
        if rows is None:
            continue
        _write_csv(run_directory / filename, header, rows)
        _add_artifact(manifest, run_directory, logical_name, filename, len(rows))

    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest_path


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

    def test_file_record_id_must_match_scan_id_and_source_path(self) -> None:
        cases = (
            (StagingPlanRow, _plan_row),
            (StagingCopyRow, _copy_row),
            (StagingErrorRow, _error_row),
        )
        for model, builder in cases:
            with self.subTest(model=model.__name__):
                row = builder(file_record_id=FILE_RECORD_ID_B)
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    "does not match scan_id and source_path",
                ):
                    model.from_csv_row(row)

    def test_stage_relative_path_must_equal_files_prefixed_source_path(self) -> None:
        cases = (
            (StagingPlanRow, _plan_row),
            (StagingCopyRow, _copy_row),
        )
        for model, builder in cases:
            with self.subTest(model=model.__name__):
                row = builder(stage_relative_path="files/other/track.flac")
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    "followed by source_path",
                ):
                    model.from_csv_row(row)

    def test_stage_relative_path_rejects_unsafe_syntax(self) -> None:
        cases = {
            "dotdot segment": ("files/../secret", "segments"),
            "embedded windows path": (r"files/C:\x", "separators"),
        }
        for label, (value, fragment) in cases.items():
            with self.subTest(label=label):
                row = _plan_row(stage_relative_path=value)
                with self.assertRaisesRegex(ArtifactValidationError, fragment):
                    StagingPlanRow.from_csv_row(row)

    def test_error_row_accepts_finalization_stage(self) -> None:
        row = _error_row(stage="finalization", error_code="cleanup_failed")
        self.assertEqual(StagingErrorRow.from_csv_row(row).to_csv_row(), row)

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

    def test_staging_plan_and_copies_reject_duplicate_keys(self) -> None:
        loaders = (
            (STAGING_PLAN_HEADER, _plan_row, load_staging_plan),
            (STAGING_COPIES_HEADER, _copy_row, load_staging_copies),
        )
        for header, builder, loader in loaders:
            with self.subTest(loader=loader.__name__, kind="file_record_id"):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    path = Path(temporary_directory) / "artifact.csv"
                    _write_csv(
                        path,
                        header,
                        [
                            builder(source_path="a/track.flac"),
                            builder(source_path="a/track.flac"),
                        ],
                    )
                    with self.assertRaisesRegex(
                        ArtifactValidationError,
                        "duplicate file_record_id",
                    ):
                        loader(path)

            with self.subTest(loader=loader.__name__, kind="source_path"):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    path = Path(temporary_directory) / "artifact.csv"
                    _write_csv(
                        path,
                        header,
                        [
                            builder(source_path="a/track.flac", scan_id=SCAN_ID),
                            builder(source_path="a/track.flac", scan_id=OTHER_SCAN_ID),
                        ],
                    )
                    with self.assertRaisesRegex(
                        ArtifactValidationError,
                        "duplicate source_path",
                    ):
                        loader(path)

    def test_staging_errors_reject_exact_duplicate_triples_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "staging_errors.csv"
            _write_csv(
                path,
                STAGING_ERRORS_HEADER,
                [
                    _error_row(
                        source_path="a/track.flac",
                        stage="copy",
                        error_code="write_failed",
                    ),
                    _error_row(
                        source_path="a/track.flac",
                        stage="copy",
                        error_code="write_failed",
                    ),
                ],
            )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "duplicate error row",
            ):
                load_staging_errors(path)

        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "staging_errors.csv"
            _write_csv(
                path,
                STAGING_ERRORS_HEADER,
                [
                    _error_row(
                        source_path="a/track.flac",
                        stage="copy",
                        error_code="write_failed",
                    ),
                    _error_row(
                        source_path="a/track.flac",
                        stage="verification",
                        error_code="digest_mismatch",
                    ),
                ],
            )
            self.assertEqual(len(load_staging_errors(path)), 2)

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

            plan_rows = [
                _plan_row(source_path="a/track.flac"),
                _plan_row(source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B),
            ]
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
            self.assertEqual(len(artifacts.staging_plan_rows), 2)
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

    def test_staging_referential_integrity_violations_are_rejected(self) -> None:
        cases = {
            "copies row references an unplanned file": (
                [_plan_row(source_path="a/track.flac")],
                [
                    _copy_row(
                        source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B
                    )
                ],
                [],
                r"staging_copies\.csv.*does not reference a staging_plan row",
            ),
            "errors row references an unplanned file": (
                [_plan_row(source_path="a/track.flac")],
                [],
                [
                    _error_row(
                        source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B
                    )
                ],
                r"staging_errors\.csv.*does not reference a staging_plan row",
            ),
            "copies row references a not_eligible plan row": (
                [
                    _plan_row(source_path="a/track.flac"),
                    _plan_row(
                        source_path="b/track.flac",
                        file_record_id=FILE_RECORD_ID_B,
                        plan_status="not_eligible",
                        reason_code="archive_row",
                    ),
                ],
                [
                    _copy_row(
                        source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B
                    )
                ],
                [],
                r"staging_copies\.csv.*not planned",
            ),
            "errors row references a not_eligible plan row": (
                [
                    _plan_row(source_path="a/track.flac"),
                    _plan_row(
                        source_path="b/track.flac",
                        file_record_id=FILE_RECORD_ID_B,
                        plan_status="not_eligible",
                        reason_code="archive_row",
                    ),
                ],
                [],
                [
                    _error_row(
                        source_path="b/track.flac", file_record_id=FILE_RECORD_ID_B
                    )
                ],
                r"staging_errors\.csv.*not planned",
            ),
            "file present in both copies and errors": (
                [_plan_row(source_path="a/track.flac")],
                [_copy_row(source_path="a/track.flac")],
                [
                    _error_row(
                        source_path="a/track.flac",
                        stage="verification",
                        error_code="digest_mismatch",
                    )
                ],
                r"staging_errors\.csv.*must not also appear in staging_copies\.csv",
            ),
        }
        for label, (plan_rows, copy_rows, error_rows, pattern) in cases.items():
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    run_directory = Path(temporary_directory) / str(SCAN_ID)
                    manifest_path = _build_staging_manifest(
                        run_directory,
                        plan_rows=plan_rows,
                        copy_rows=copy_rows,
                        error_rows=error_rows,
                    )
                    with self.assertRaisesRegex(ArtifactValidationError, pattern):
                        validate_artifact_set(manifest_path)

    def test_staging_scan_id_and_stage_id_consistency_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(SCAN_ID)
            manifest_path = _build_staging_manifest(
                run_directory,
                plan_rows=[
                    _plan_row(scan_id=OTHER_SCAN_ID, source_path="a/track.flac")
                ],
            )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "does not match the manifest",
            ):
                validate_artifact_set(manifest_path)

        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(SCAN_ID)
            manifest_path = _build_staging_manifest(
                run_directory,
                plan_rows=[_plan_row(source_path="a/track.flac")],
                copy_rows=[
                    _copy_row(source_path="a/track.flac", stage_id=OTHER_STAGE_ID)
                ],
                error_rows=[],
            )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "must be consistent across the registered staging family",
            ):
                validate_artifact_set(manifest_path)

    def test_staging_and_matching_families_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(SCAN_ID)
            album_group_id = uuid5(
                SCAN_ID,
                "musicbrainz-album-group-v1\0synthetic album artist\0synthetic album",
            )
            matching_specs = (
                (
                    "musicbrainz_album_groups",
                    "musicbrainz_album_groups.csv",
                    MUSICBRAINZ_ALBUM_GROUPS_HEADER,
                    [
                        {
                            "scan_id": str(SCAN_ID),
                            "album_group_id": str(album_group_id),
                            "file_record_id": str(LIBRARY_FILE_RECORD_ID),
                        }
                    ],
                ),
                (
                    "musicbrainz_album_candidates",
                    "musicbrainz_album_candidates.csv",
                    MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
                    [
                        {
                            "scan_id": str(SCAN_ID),
                            "album_group_id": str(album_group_id),
                            "candidate_rank": "1",
                            "release_group_mbid": (
                                "11111111-1111-4111-8111-111111111111"
                            ),
                            "title": "Synthetic Album",
                            "artist_credit": "Synthetic Album Artist",
                            "first_release_date": "2024-01-02",
                            "primary_type": "Album",
                            "secondary_types_json": "[]",
                            "musicbrainz_search_score": "100",
                            "title_similarity": "1.0000",
                            "artist_similarity": "1.0000",
                            "year_similarity": "1.0000",
                            "confidence_score": "100.00",
                        }
                    ],
                ),
                (
                    "musicbrainz_recording_candidates",
                    "musicbrainz_recording_candidates.csv",
                    MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
                    [
                        {
                            "scan_id": str(SCAN_ID),
                            "file_record_id": str(LIBRARY_FILE_RECORD_ID),
                            "candidate_rank": "1",
                            "recording_mbid": ("22222222-2222-4222-8222-222222222222"),
                            "title": "Synthetic Track",
                            "artist_credit": "Synthetic Artist",
                            "duration_ms": "201250",
                            "first_release_date": "2024",
                            "matched_release_mbid": (
                                "33333333-3333-4333-8333-333333333333"
                            ),
                            "matched_release_title": "Synthetic Album",
                            "musicbrainz_search_score": "99",
                            "title_similarity": "1.0000",
                            "artist_similarity": "1.0000",
                            "duration_similarity": "1.0000",
                            "album_similarity": "1.0000",
                            "confidence_score": "99.95",
                        }
                    ],
                ),
                (
                    "musicbrainz_match_results",
                    "musicbrainz_match_results.csv",
                    MUSICBRAINZ_MATCH_RESULTS_HEADER,
                    [
                        {
                            "scan_id": str(SCAN_ID),
                            "subject_type": "album",
                            "subject_id": str(album_group_id),
                            "status": "matched",
                            "candidate_count": "1",
                            "top_candidate_mbid": (
                                "11111111-1111-4111-8111-111111111111"
                            ),
                            "top_confidence_score": "100.00",
                            "confidence_margin": "100.00",
                            "reason_code": "high_confidence_with_margin",
                        },
                        {
                            "scan_id": str(SCAN_ID),
                            "subject_type": "recording",
                            "subject_id": str(LIBRARY_FILE_RECORD_ID),
                            "status": "matched",
                            "candidate_count": "1",
                            "top_candidate_mbid": (
                                "22222222-2222-4222-8222-222222222222"
                            ),
                            "top_confidence_score": "99.95",
                            "confidence_margin": "99.95",
                            "reason_code": "high_confidence_with_margin",
                        },
                    ],
                ),
            )
            manifest_path = _build_staging_manifest(
                run_directory,
                plan_rows=[_plan_row(source_path="a/track.flac")],
                copy_rows=[_copy_row(source_path="a/track.flac")],
                error_rows=[],
                extra_specs=matching_specs,
            )

            artifacts = validate_artifact_set(manifest_path)
            self.assertEqual(len(artifacts.musicbrainz_album_group_rows), 1)
            self.assertEqual(len(artifacts.musicbrainz_album_candidate_rows), 1)
            self.assertEqual(len(artifacts.musicbrainz_recording_candidate_rows), 1)
            self.assertEqual(len(artifacts.musicbrainz_match_result_rows), 2)
            self.assertEqual(len(artifacts.staging_plan_rows), 1)
            self.assertEqual(len(artifacts.staging_copy_rows), 1)
            self.assertEqual(len(artifacts.staging_error_rows), 0)


if __name__ == "__main__":
    unittest.main()
