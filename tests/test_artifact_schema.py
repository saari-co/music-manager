"""Tests for the standalone v0.3 schema 1 contract machinery."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_manager.artifact_schema import (
    LIBRARY_SCAN_HEADER,
    SCAN_ERRORS_HEADER,
    ArtifactValidationError,
    LibraryScanRow,
    ScanErrorRow,
    ScanManifest,
    UnsupportedSchemaVersionError,
    load_library_scan,
    load_scan_errors,
    load_scan_manifest,
    make_file_fingerprint,
    make_file_record_id,
    validate_artifact_set,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3"
VALID_FIXTURES = FIXTURES / "valid"
INVALID_FIXTURES = FIXTURES / "invalid"


def _manifest_data() -> dict:
    return json.loads(
        (VALID_FIXTURES / "scan_manifest.json").read_text(encoding="utf-8")
    )


def _library_row(index: int = 0) -> dict[str, str]:
    with (VALID_FIXTURES / "library_scan.csv").open(
        encoding="utf-8", newline=""
    ) as report:
        return list(csv.DictReader(report))[index]


def _error_row() -> dict[str, str]:
    with (VALID_FIXTURES / "scan_errors.csv").open(
        encoding="utf-8", newline=""
    ) as report:
        return next(csv.DictReader(report))


def _write_manifest(directory: Path, data: dict) -> None:
    (directory / "scan_manifest.json").write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )


def _replace_artifact_digest(
    directory: Path, manifest: dict, logical_name: str
) -> None:
    filename = manifest["artifacts"][logical_name]["filename"]
    payload = (directory / filename).read_bytes()
    manifest["artifacts"][logical_name]["sha256"] = hashlib.sha256(payload).hexdigest()


class SchemaOneModelTests(unittest.TestCase):
    """Validate exact model representations without runtime integration."""

    def test_exact_ordered_headers(self) -> None:
        self.assertEqual(
            LIBRARY_SCAN_HEADER,
            (
                "scan_id",
                "file_record_id",
                "file_fingerprint",
                "path",
                "extension",
                "file_type",
                "file_size_bytes",
                "modified_time_ns",
                "artist",
                "album_artist",
                "title",
                "album",
                "date",
                "release_year",
                "track_number",
                "track_total",
                "disc_number",
                "disc_total",
                "genre",
                "composer",
                "is_compilation",
                "codec",
                "container",
                "bitrate_kbps",
                "duration_seconds",
                "sample_rate_hz",
                "bit_depth",
                "channels",
                "record_status",
            ),
        )
        self.assertEqual(
            SCAN_ERRORS_HEADER,
            (
                "scan_id",
                "file_record_id",
                "path",
                "stage",
                "severity",
                "error_code",
                "message",
            ),
        )

    def test_golden_models_round_trip_without_coercion(self) -> None:
        manifest_data = _manifest_data()
        manifest = ScanManifest.from_dict(manifest_data)
        self.assertEqual(manifest.to_dict(), manifest_data)

        with (VALID_FIXTURES / "library_scan.csv").open(
            encoding="utf-8", newline=""
        ) as report:
            source_library_rows = list(csv.DictReader(report))
        library_rows = load_library_scan(VALID_FIXTURES / "library_scan.csv")
        self.assertEqual(
            [row.to_csv_row() for row in library_rows],
            source_library_rows,
        )

        with (VALID_FIXTURES / "scan_errors.csv").open(
            encoding="utf-8", newline=""
        ) as report:
            source_error_rows = list(csv.DictReader(report))
        error_rows = load_scan_errors(VALID_FIXTURES / "scan_errors.csv")
        self.assertEqual(
            [row.to_csv_row() for row in error_rows],
            source_error_rows,
        )

    def test_schema_version_compatibility_boundaries(self) -> None:
        compatible = _manifest_data()
        compatible["schema_version"] = "1.0.9"
        self.assertEqual(
            ScanManifest.from_dict(compatible).schema_version,
            "1.0.9",
        )

        for version in ("0.9.0", "1.1.0", "2.0.0"):
            with self.subTest(version=version):
                invalid = _manifest_data()
                invalid["schema_version"] = version
                with self.assertRaises(UnsupportedSchemaVersionError):
                    ScanManifest.from_dict(invalid)

        for version in ("1", "1.0", "01.0.0", "1.0.0-alpha"):
            with self.subTest(version=version):
                invalid = _manifest_data()
                invalid["schema_version"] = version
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    "major.minor.patch",
                ):
                    ScanManifest.from_dict(invalid)

    def test_manifest_rejects_unknown_fields_and_invalid_state_values(self) -> None:
        cases = {
            "unknown field": lambda value: value.update({"root": "/private"}),
            "absolute mode": lambda value: value["configuration"].update(
                {"path_mode": "absolute"}
            ),
            "follow symlinks": lambda value: value["configuration"].update(
                {"follow_symlinks": True}
            ),
            "non-UTC timestamp": lambda value: value.update(
                {"completed_at": "2026-07-04T12:00:01-04:00"}
            ),
            "bad state": lambda value: value.update({"state": "partial"}),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                invalid = _manifest_data()
                mutate(invalid)
                with self.assertRaises(ArtifactValidationError):
                    ScanManifest.from_dict(invalid)

    def test_manifest_rejects_duplicate_json_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manifest_path = Path(temporary_directory) / "scan_manifest.json"
            manifest_path.write_text(
                '{"schema_version":"1.0.0","schema_version":"1.0.0"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "duplicate JSON field",
            ):
                load_scan_manifest(manifest_path)

    def test_manifest_rejects_completion_before_start(self) -> None:
        invalid = _manifest_data()
        invalid["started_at"] = "2026-07-04T16:00:10Z"
        invalid["completed_at"] = "2026-07-04T16:00:01Z"

        with self.assertRaisesRegex(
            ArtifactValidationError,
            "greater than or equal to started_at",
        ):
            ScanManifest.from_dict(invalid)

        equal = _manifest_data()
        equal["completed_at"] = equal["started_at"]
        self.assertEqual(
            ScanManifest.from_dict(equal).completed_at,
            equal["started_at"],
        )

    def test_derived_artifact_requires_sanitized_configuration(self) -> None:
        data = _manifest_data()
        data["artifacts"]["library_analysis"] = {
            "filename": "library_analysis.csv",
            "role": "derived",
            "application_version": "0.3.0",
            "generated_at": "2026-07-04T16:01:00Z",
            "row_count": 1,
            "sha256": "0" * 64,
            "configuration": {"duration_tolerance": 3},
        }
        manifest = ScanManifest.from_dict(data)
        self.assertEqual(
            manifest.artifacts["library_analysis"].configuration,
            {"duration_tolerance": 3},
        )

        del data["artifacts"]["library_analysis"]["configuration"]
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "configuration",
        ):
            ScanManifest.from_dict(data)

    def test_record_id_and_fingerprint_helpers_encode_contract(self) -> None:
        row = _library_row()
        scan_id = ScanManifest.from_dict(_manifest_data()).scan_id
        self.assertEqual(
            str(make_file_record_id(scan_id, row["path"])),
            row["file_record_id"],
        )
        self.assertEqual(
            make_file_fingerprint(
                int(row["file_size_bytes"]),
                int(row["modified_time_ns"]),
            ),
            row["file_fingerprint"],
        )

    def test_library_row_rejects_invalid_value_representations(self) -> None:
        cases = {
            "absolute path": ("path", "/private/Track.flac"),
            "parent traversal": ("path", "Artist/../Track.flac"),
            "backslash path": ("path", r"Artist\Track.flac"),
            "uppercase extension": ("extension", ".FLAC"),
            "unknown file type": ("file_type", "document"),
            "grouped integer": ("file_size_bytes", "123,456"),
            "decimal integer": ("modified_time_ns", "1.0"),
            "surrounding tag whitespace": ("artist", " Artist "),
            "negative parsed number": ("track_number", "-1"),
            "noncanonical boolean": ("is_compilation", "False"),
            "exponent decimal": ("bitrate_kbps", "9e2"),
            "NaN decimal": ("duration_seconds", "NaN"),
            "fractional integer": ("sample_rate_hz", "44100.0"),
            "unknown status": ("record_status", "readable"),
            "bad fingerprint": ("file_fingerprint", "stat-v1:" + "0" * 64),
            "noncanonical scan UUID": (
                "scan_id",
                "12345678-1234-4ABC-8DEF-1234567890AB",
            ),
        }
        for label, (field, replacement) in cases.items():
            with self.subTest(label=label):
                row = _library_row()
                row[field] = replacement
                with self.assertRaises(ArtifactValidationError):
                    LibraryScanRow.from_csv_row(row)

    def test_library_row_rejects_partial_or_unexplained_stat_nulls(self) -> None:
        partial = _library_row()
        partial["modified_time_ns"] = ""
        partial["file_fingerprint"] = ""
        partial["record_status"] = "error"
        with self.assertRaisesRegex(ArtifactValidationError, "both be set"):
            LibraryScanRow.from_csv_row(partial)

        unexplained = _library_row()
        unexplained["file_size_bytes"] = ""
        unexplained["modified_time_ns"] = ""
        unexplained["file_fingerprint"] = ""
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "must be error",
        ):
            LibraryScanRow.from_csv_row(unexplained)

    def test_row_models_reject_missing_extra_and_non_string_fields(self) -> None:
        missing = _library_row()
        del missing["artist"]
        with self.assertRaisesRegex(ArtifactValidationError, "missing fields"):
            LibraryScanRow.from_csv_row(missing)

        extra = _library_row()
        extra["unexpected"] = ""
        with self.assertRaisesRegex(ArtifactValidationError, "unexpected fields"):
            LibraryScanRow.from_csv_row(extra)

        wrong_type = _library_row()
        wrong_type["track_number"] = 1  # type: ignore[assignment]
        with self.assertRaisesRegex(ArtifactValidationError, "must be a string"):
            LibraryScanRow.from_csv_row(wrong_type)

    def test_error_row_rejects_invalid_machine_values(self) -> None:
        cases = {
            "unknown stage": ("stage", "walking"),
            "unknown severity": ("severity", "warning"),
            "bad code": ("error_code", "Symlink Skipped"),
            "empty message": ("message", ""),
        }
        for label, (field, replacement) in cases.items():
            with self.subTest(label=label):
                row = _error_row()
                row[field] = replacement
                with self.assertRaises(ArtifactValidationError):
                    ScanErrorRow.from_csv_row(row)

        linked_without_path = _error_row()
        linked_without_path["file_record_id"] = _library_row()["file_record_id"]
        linked_without_path["path"] = ""
        linked_without_path["error_code"] = "metadata_read_failed"
        linked_without_path["severity"] = "error"
        with self.assertRaisesRegex(ArtifactValidationError, "path"):
            ScanErrorRow.from_csv_row(linked_without_path)

        bad_symlink_severity = _error_row()
        bad_symlink_severity["severity"] = "error"
        with self.assertRaisesRegex(ArtifactValidationError, "info severity"):
            ScanErrorRow.from_csv_row(bad_symlink_severity)


class CsvShapeTests(unittest.TestCase):
    """Reject malformed CSV structure before consumers see row values."""

    def test_focused_invalid_fixtures_are_rejected(self) -> None:
        cases = (
            (
                load_library_scan,
                "library_scan_duplicate_header.csv",
                "duplicate columns",
            ),
            (
                load_library_scan,
                "library_scan_exponent_decimal.csv",
                "without an exponent",
            ),
            (
                load_scan_errors,
                "scan_errors_invalid_stage.csv",
                "must be one of",
            ),
        )
        for loader, filename, message in cases:
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(
                    ArtifactValidationError,
                    message,
                ):
                    loader(INVALID_FIXTURES / filename)

    def test_missing_extra_reordered_headers_and_row_width_are_rejected(
        self,
    ) -> None:
        valid_lines = (
            (VALID_FIXTURES / "library_scan.csv")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        headers = valid_lines[0].split(",")
        cases = {
            "missing": ",".join(headers[:-1]),
            "extra": f"{valid_lines[0]},unexpected",
            "reordered": ",".join([headers[1], headers[0], *headers[2:]]),
            "short row": valid_lines[0]
            + "\n"
            + ",".join(valid_lines[1].split(",")[:-1]),
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for label, content in cases.items():
                with self.subTest(label=label):
                    path = directory / f"{label}.csv"
                    if label != "short row":
                        content = f"{content}\n{valid_lines[1]}\n"
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ArtifactValidationError):
                        load_library_scan(path)


class ArtifactSetValidationTests(unittest.TestCase):
    """Validate cross-file identity, count, and integrity relationships."""

    def test_golden_artifact_set_is_valid(self) -> None:
        validated = validate_artifact_set(VALID_FIXTURES / "scan_manifest.json")
        self.assertEqual(validated.manifest.state, "complete")
        self.assertEqual(len(validated.library_rows), 2)
        self.assertEqual(len(validated.error_rows), 1)

    def test_validation_opens_only_registered_artifacts(self) -> None:
        allowed = {
            VALID_FIXTURES / "scan_manifest.json",
            VALID_FIXTURES / "library_scan.csv",
            VALID_FIXTURES / "scan_errors.csv",
        }
        opened: list[Path] = []
        original_open = Path.open

        def guarded_open(path: Path, *args: object, **kwargs: object):
            opened.append(path)
            if path not in allowed:
                raise AssertionError(f"unexpected path access: {path}")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            validate_artifact_set(VALID_FIXTURES / "scan_manifest.json")

        self.assertEqual(set(opened), allowed)
        self.assertNotIn(
            VALID_FIXTURES / "Artist/Album/01 Track.flac",
            opened,
        )

    def test_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "run"
            shutil.copytree(VALID_FIXTURES, directory)
            with (directory / "library_scan.csv").open("a", encoding="utf-8") as report:
                report.write("\n")
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "digest does not match",
            ):
                validate_artifact_set(directory / "scan_manifest.json")

    def test_manifest_row_count_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "run"
            shutil.copytree(VALID_FIXTURES, directory)
            manifest = _manifest_data()
            manifest["counts"]["inventory_rows"] = 3
            manifest["artifacts"]["library_scan"]["row_count"] = 3
            _write_manifest(directory, manifest)
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "row count 2",
            ):
                validate_artifact_set(directory / "scan_manifest.json")

    def test_cross_artifact_scan_id_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "run"
            shutil.copytree(VALID_FIXTURES, directory)
            error_path = directory / "scan_errors.csv"
            with error_path.open(encoding="utf-8", newline="") as report:
                rows = list(csv.DictReader(report))
            rows[0]["scan_id"] = "87654321-4321-4abc-8def-1234567890ab"
            with error_path.open("w", encoding="utf-8", newline="") as report:
                writer = csv.DictWriter(report, fieldnames=SCAN_ERRORS_HEADER)
                writer.writeheader()
                writer.writerows(rows)
            manifest = _manifest_data()
            _replace_artifact_digest(directory, manifest, "scan_errors")
            _write_manifest(directory, manifest)

            with self.assertRaisesRegex(
                ArtifactValidationError,
                "does not match the manifest",
            ):
                validate_artifact_set(directory / "scan_manifest.json")

    def test_manifest_finding_counts_must_match_error_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "run"
            shutil.copytree(VALID_FIXTURES, directory)
            manifest = _manifest_data()
            manifest["counts"]["info_findings"] = 0
            manifest["counts"]["error_findings"] = 1
            manifest["counts"]["skipped_symlinks"] = 0
            manifest["state"] = "incomplete"
            _write_manifest(directory, manifest)
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "finding severities",
            ):
                validate_artifact_set(directory / "scan_manifest.json")

    def test_ok_record_cannot_have_linked_error_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "run"
            shutil.copytree(VALID_FIXTURES, directory)
            library_row = _library_row()
            error_path = directory / "scan_errors.csv"
            error_row = {
                "scan_id": library_row["scan_id"],
                "file_record_id": library_row["file_record_id"],
                "path": library_row["path"],
                "stage": "metadata",
                "severity": "error",
                "error_code": "metadata_read_failed",
                "message": "Synthetic metadata failure",
            }
            with error_path.open("w", encoding="utf-8", newline="") as report:
                writer = csv.DictWriter(report, fieldnames=SCAN_ERRORS_HEADER)
                writer.writeheader()
                writer.writerow(error_row)

            manifest = _manifest_data()
            manifest["state"] = "incomplete"
            manifest["counts"]["info_findings"] = 0
            manifest["counts"]["error_findings"] = 1
            manifest["counts"]["skipped_symlinks"] = 0
            _replace_artifact_digest(directory, manifest, "scan_errors")
            _write_manifest(directory, manifest)

            with self.assertRaisesRegex(
                ArtifactValidationError,
                "must be error when linked",
            ):
                validate_artifact_set(directory / "scan_manifest.json")


if __name__ == "__main__":
    unittest.main()
