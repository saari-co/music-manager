"""Schema 1.1 MusicBrainz artifact model and relationship tests."""

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
    MATCHING_ARTIFACT_NAMES,
    MATCHING_SCHEMA_VERSION,
    MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
    MUSICBRAINZ_ALBUM_GROUPS_HEADER,
    MUSICBRAINZ_MATCH_RESULTS_HEADER,
    MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
    SCHEMA_VERSION,
    ArtifactValidationError,
    MusicBrainzAlbumCandidateRow,
    MusicBrainzAlbumGroupRow,
    MusicBrainzMatchResultRow,
    MusicBrainzRecordingCandidateRow,
    ScanManifest,
    load_musicbrainz_album_candidates,
    load_musicbrainz_album_groups,
    load_musicbrainz_match_results,
    load_musicbrainz_recording_candidates,
    validate_artifact_set,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
FILE_RECORD_ID = UUID("aec6a2b3-b8d7-55ea-a953-25d4c1a793dd")
ALBUM_GROUP_ID = uuid5(
    SCAN_ID,
    "musicbrainz-album-group-v1\0synthetic album artist\0synthetic album",
)
RELEASE_GROUP_MBID = UUID("11111111-1111-4111-8111-111111111111")
RECORDING_MBID = UUID("22222222-2222-4222-8222-222222222222")
RELEASE_MBID = UUID("33333333-3333-4333-8333-333333333333")


def _album_group_row() -> dict[str, str]:
    return {
        "scan_id": str(SCAN_ID),
        "album_group_id": str(ALBUM_GROUP_ID),
        "file_record_id": str(FILE_RECORD_ID),
    }


def _album_candidate_row() -> dict[str, str]:
    return {
        "scan_id": str(SCAN_ID),
        "album_group_id": str(ALBUM_GROUP_ID),
        "candidate_rank": "1",
        "release_group_mbid": str(RELEASE_GROUP_MBID),
        "title": "Synthetic Album",
        "artist_credit": "Synthetic Album Artist",
        "first_release_date": "2024-01-02",
        "primary_type": "Album",
        "secondary_types_json": '["Compilation","Soundtrack"]',
        "musicbrainz_search_score": "100",
        "title_similarity": "1.0000",
        "artist_similarity": "1.0000",
        "year_similarity": "1.0000",
        "confidence_score": "100.00",
    }


def _recording_candidate_row() -> dict[str, str]:
    return {
        "scan_id": str(SCAN_ID),
        "file_record_id": str(FILE_RECORD_ID),
        "candidate_rank": "1",
        "recording_mbid": str(RECORDING_MBID),
        "title": "Synthetic Track",
        "artist_credit": "Synthetic Artist",
        "duration_ms": "201250",
        "first_release_date": "2024",
        "matched_release_mbid": str(RELEASE_MBID),
        "matched_release_title": "Synthetic Album",
        "musicbrainz_search_score": "99",
        "title_similarity": "1.0000",
        "artist_similarity": "1.0000",
        "duration_similarity": "1.0000",
        "album_similarity": "1.0000",
        "confidence_score": "99.95",
    }


def _result_rows() -> list[dict[str, str]]:
    return [
        {
            "scan_id": str(SCAN_ID),
            "subject_type": "album",
            "subject_id": str(ALBUM_GROUP_ID),
            "status": "matched",
            "candidate_count": "1",
            "top_candidate_mbid": str(RELEASE_GROUP_MBID),
            "top_confidence_score": "100.00",
            "confidence_margin": "100.00",
            "reason_code": "high_confidence_with_margin",
        },
        {
            "scan_id": str(SCAN_ID),
            "subject_type": "recording",
            "subject_id": str(FILE_RECORD_ID),
            "status": "matched",
            "candidate_count": "1",
            "top_candidate_mbid": str(RECORDING_MBID),
            "top_confidence_score": "99.95",
            "confidence_margin": "99.95",
            "reason_code": "high_confidence_with_margin",
        },
    ]


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
        "application_version": "0.3.0",
        "generated_at": "2026-07-05T12:00:00Z",
        "row_count": row_count,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "configuration": {
            "client_policy_version": "musicbrainz-client-v1",
            "scoring_model_version": "musicbrainz-confidence-v1",
        },
    }


class MusicBrainzSchemaModelTests(unittest.TestCase):
    def test_matching_adds_reader_support_without_changing_scan_output_version(
        self,
    ) -> None:
        self.assertEqual(SCHEMA_VERSION, "1.0.0")
        self.assertEqual(MATCHING_SCHEMA_VERSION, "1.1.0")

    def test_exact_ordered_headers_match_the_contract(self) -> None:
        self.assertEqual(
            MUSICBRAINZ_ALBUM_GROUPS_HEADER,
            ("scan_id", "album_group_id", "file_record_id"),
        )
        self.assertEqual(
            MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
            (
                "scan_id",
                "album_group_id",
                "candidate_rank",
                "release_group_mbid",
                "title",
                "artist_credit",
                "first_release_date",
                "primary_type",
                "secondary_types_json",
                "musicbrainz_search_score",
                "title_similarity",
                "artist_similarity",
                "year_similarity",
                "confidence_score",
            ),
        )
        self.assertEqual(
            MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
            (
                "scan_id",
                "file_record_id",
                "candidate_rank",
                "recording_mbid",
                "title",
                "artist_credit",
                "duration_ms",
                "first_release_date",
                "matched_release_mbid",
                "matched_release_title",
                "musicbrainz_search_score",
                "title_similarity",
                "artist_similarity",
                "duration_similarity",
                "album_similarity",
                "confidence_score",
            ),
        )
        self.assertEqual(
            MUSICBRAINZ_MATCH_RESULTS_HEADER,
            (
                "scan_id",
                "subject_type",
                "subject_id",
                "status",
                "candidate_count",
                "top_candidate_mbid",
                "top_confidence_score",
                "confidence_margin",
                "reason_code",
            ),
        )

    def test_rows_round_trip_without_coercion(self) -> None:
        cases = (
            (MusicBrainzAlbumGroupRow, _album_group_row()),
            (MusicBrainzAlbumCandidateRow, _album_candidate_row()),
            (MusicBrainzRecordingCandidateRow, _recording_candidate_row()),
            (MusicBrainzMatchResultRow, _result_rows()[0]),
        )
        for model, source in cases:
            with self.subTest(model=model.__name__):
                self.assertEqual(model.from_csv_row(source).to_csv_row(), source)

    def test_candidate_rows_reject_noncanonical_values(self) -> None:
        album_cases = {
            "zero rank": ("candidate_rank", "0"),
            "score over 100": ("musicbrainz_search_score", "101"),
            "short similarity": ("title_similarity", "1.000"),
            "similarity over one": ("artist_similarity", "1.0001"),
            "noncanonical JSON": (
                "secondary_types_json",
                '["Soundtrack", "Compilation"]',
            ),
            "invalid date": ("first_release_date", "2024-02-30"),
        }
        for label, (field, replacement) in album_cases.items():
            with self.subTest(label=label):
                row = _album_candidate_row()
                row[field] = replacement
                with self.assertRaises(ArtifactValidationError):
                    MusicBrainzAlbumCandidateRow.from_csv_row(row)

        recording = _recording_candidate_row()
        recording["matched_release_title"] = ""
        with self.assertRaisesRegex(ArtifactValidationError, "both be set"):
            MusicBrainzRecordingCandidateRow.from_csv_row(recording)

    def test_match_results_require_consistent_candidate_fields(self) -> None:
        result = _result_rows()[0]
        result["candidate_count"] = "0"
        with self.assertRaisesRegex(ArtifactValidationError, "zero candidates"):
            MusicBrainzMatchResultRow.from_csv_row(result)

        result = _result_rows()[0]
        result["top_confidence_score"] = ""
        with self.assertRaisesRegex(ArtifactValidationError, "all be set"):
            MusicBrainzMatchResultRow.from_csv_row(result)

    def test_loaders_reject_reordered_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "musicbrainz_album_groups.csv"
            path.write_text(
                "album_group_id,scan_id,file_record_id\n"
                f"{ALBUM_GROUP_ID},{SCAN_ID},{FILE_RECORD_ID}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ArtifactValidationError,
                "required order",
            ):
                load_musicbrainz_album_groups(path)


class MusicBrainzArtifactSetTests(unittest.TestCase):
    def test_schema_1_1_matching_family_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / str(SCAN_ID)
            shutil.copytree(FIXTURES, run_directory)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "1.1.0"

            specs = (
                (
                    "musicbrainz_album_groups",
                    "musicbrainz_album_groups.csv",
                    MUSICBRAINZ_ALBUM_GROUPS_HEADER,
                    [_album_group_row()],
                ),
                (
                    "musicbrainz_album_candidates",
                    "musicbrainz_album_candidates.csv",
                    MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
                    [_album_candidate_row()],
                ),
                (
                    "musicbrainz_recording_candidates",
                    "musicbrainz_recording_candidates.csv",
                    MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
                    [_recording_candidate_row()],
                ),
                (
                    "musicbrainz_match_results",
                    "musicbrainz_match_results.csv",
                    MUSICBRAINZ_MATCH_RESULTS_HEADER,
                    _result_rows(),
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

            self.assertEqual(
                len(artifacts.musicbrainz_album_group_rows),
                1,
            )
            self.assertEqual(
                len(artifacts.musicbrainz_album_candidate_rows),
                1,
            )
            self.assertEqual(
                len(artifacts.musicbrainz_recording_candidate_rows),
                1,
            )
            self.assertEqual(
                len(artifacts.musicbrainz_match_result_rows),
                2,
            )

    def test_schema_1_0_rejects_matching_artifact_names(self) -> None:
        manifest = json.loads(
            (FIXTURES / "scan_manifest.json").read_text(encoding="utf-8")
        )
        for logical_name in MATCHING_ARTIFACT_NAMES:
            manifest["artifacts"][logical_name] = {
                "filename": f"{logical_name}.csv",
                "role": "derived",
                "application_version": "0.3.0",
                "generated_at": "2026-07-05T12:00:00Z",
                "row_count": 0,
                "sha256": "0" * 64,
                "configuration": {},
            }
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "requires schema 1.1",
        ):
            ScanManifest.from_dict(manifest)

    def test_schema_1_1_requires_the_complete_matching_family(self) -> None:
        manifest = json.loads(
            (FIXTURES / "scan_manifest.json").read_text(encoding="utf-8")
        )
        manifest["schema_version"] = "1.1.0"
        manifest["artifacts"]["musicbrainz_match_results"] = {
            "filename": "musicbrainz_match_results.csv",
            "role": "derived",
            "application_version": "0.3.0",
            "generated_at": "2026-07-05T12:00:00Z",
            "row_count": 0,
            "sha256": "0" * 64,
            "configuration": {},
        }
        with self.assertRaisesRegex(
            ArtifactValidationError,
            "matching artifact family is missing",
        ):
            ScanManifest.from_dict(manifest)

    def test_public_loaders_accept_header_only_reports(self) -> None:
        cases = (
            (load_musicbrainz_album_groups, MUSICBRAINZ_ALBUM_GROUPS_HEADER),
            (
                load_musicbrainz_album_candidates,
                MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
            ),
            (
                load_musicbrainz_recording_candidates,
                MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
            ),
            (load_musicbrainz_match_results, MUSICBRAINZ_MATCH_RESULTS_HEADER),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for index, (loader, header) in enumerate(cases):
                with self.subTest(loader=loader.__name__):
                    path = directory / f"{index}.csv"
                    path.write_text(f"{','.join(header)}\n", encoding="utf-8")
                    self.assertEqual(loader(path), ())


if __name__ == "__main__":
    unittest.main()
