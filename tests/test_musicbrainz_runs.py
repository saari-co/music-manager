"""Tests for deterministic MusicBrainz artifact writing and registration."""

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

from music_manager.analysis_runs import analyze_scan_run
from music_manager.artifact_schema import (
    MATCHING_ARTIFACT_NAMES,
    MATCHING_SCHEMA_VERSION,
    MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
    MUSICBRAINZ_ALBUM_GROUPS_HEADER,
    MUSICBRAINZ_MATCH_RESULTS_HEADER,
    MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
    load_scan_manifest,
    make_file_record_id,
    validate_artifact_set,
)
from music_manager.matcher import RecordingSearchResult, ReleaseGroupSearchResult
from music_manager.musicbrainz_runs import register_musicbrainz_artifacts
from music_manager.musicbrainz_scoring import score_musicbrainz_candidates
from music_manager.musicbrainz_subjects import (
    AlbumCandidateValues,
    MusicBrainzCandidateRetrieval,
    RecordingCandidateValues,
    extract_musicbrainz_subjects,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
MATCHING_FILENAMES = {
    "musicbrainz_album_groups.csv",
    "musicbrainz_album_candidates.csv",
    "musicbrainz_recording_candidates.csv",
    "musicbrainz_match_results.csv",
}
HEADERS = {
    "musicbrainz_album_groups.csv": MUSICBRAINZ_ALBUM_GROUPS_HEADER,
    "musicbrainz_album_candidates.csv": MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
    "musicbrainz_recording_candidates.csv": (MUSICBRAINZ_RECORDING_CANDIDATES_HEADER),
    "musicbrainz_match_results.csv": MUSICBRAINZ_MATCH_RESULTS_HEADER,
}


def _clock(second: int):
    return lambda: datetime(
        2026,
        7,
        5,
        18,
        0,
        second,
        tzinfo=timezone.utc,
    )


def _copy_valid_run(root: Path) -> Path:
    run_directory = root / str(SCAN_ID)
    shutil.copytree(FIXTURES, run_directory)
    return run_directory


def _matching_inputs(
    run_directory: Path,
    *,
    reverse: bool = False,
    empty: bool = False,
):
    artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
    subjects = extract_musicbrainz_subjects(artifacts)
    album_subject = subjects.albums[0]
    recording_subject = subjects.recordings[0]
    album_candidates = (
        ReleaseGroupSearchResult(
            mbid=UUID("11111111-1111-4111-8111-111111111111"),
            title="Synthetic Album",
            artist_credit="Synthetic Album Artist",
            first_release_date="2024-01-02",
            primary_type="Album",
            secondary_types=("Compilation", "Soundtrack"),
            search_score=100,
        ),
        ReleaseGroupSearchResult(
            mbid=UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"),
            title="Synthetic, Album?",
            artist_credit="Synthetic Album Artist",
            search_score=42,
        ),
    )
    recording_candidates = (
        RecordingSearchResult(
            mbid=UUID("22222222-2222-4222-8222-222222222222"),
            title="Synthetic Track",
            artist_credit="Synthetic Artist",
            duration_ms=201_250,
            first_release_date="2024",
            releases=(
                (
                    UUID("33333333-3333-4333-8333-333333333333"),
                    "Synthetic Album",
                ),
            ),
            search_score=99,
        ),
        RecordingSearchResult(
            mbid=UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd"),
            title="Different Track",
            artist_credit="Synthetic Artist",
            duration_ms=None,
            search_score=35,
        ),
    )
    if empty:
        album_candidates = ()
        recording_candidates = ()
    elif reverse:
        album_candidates = tuple(reversed(album_candidates))
        recording_candidates = tuple(reversed(recording_candidates))
    retrieval = MusicBrainzCandidateRetrieval(
        scan_id=subjects.scan_id,
        albums=(
            AlbumCandidateValues(
                album_group_id=album_subject.album_group_id,
                candidates=album_candidates,
            ),
        ),
        recordings=(
            RecordingCandidateValues(
                file_record_id=recording_subject.file_record_id,
                candidates=recording_candidates,
            ),
        ),
    )
    return subjects, score_musicbrainz_candidates(subjects, retrieval)


def _register(
    run_directory: Path,
    *,
    second: int = 1,
    reverse: bool = False,
    empty: bool = False,
):
    subjects, scoring = _matching_inputs(
        run_directory,
        reverse=reverse,
        empty=empty,
    )
    return register_musicbrainz_artifacts(
        run_directory,
        subjects,
        scoring,
        consent_source="cli",
        clock=_clock(second),
    )


def _matching_bytes(run_directory: Path) -> dict[str, bytes]:
    return {
        filename: (run_directory / filename).read_bytes()
        for filename in MATCHING_FILENAMES
    }


def _reverse_inventory_rows(run_directory: Path) -> None:
    library_path = run_directory / "library_scan.csv"
    with library_path.open(encoding="utf-8", newline="") as report:
        reader = csv.DictReader(report)
        rows = list(reader)
        header = tuple(reader.fieldnames or ())
    with library_path.open("w", encoding="utf-8", newline="") as report:
        writer = csv.DictWriter(
            report,
            fieldnames=header,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(reversed(rows))
    manifest_path = run_directory / "scan_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["library_scan"]["sha256"] = hashlib.sha256(
        library_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


class MusicBrainzArtifactRunTests(unittest.TestCase):
    def test_registers_strict_complete_family_and_upgrades_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            original = load_scan_manifest(manifest_path)
            primary_state = {
                name: (
                    (run_directory / entry.filename).read_bytes(),
                    (run_directory / entry.filename).stat().st_mtime_ns,
                )
                for name, entry in original.artifacts.items()
            }

            outcome = _register(run_directory)

            self.assertEqual(outcome.directory, run_directory)
            self.assertEqual(
                outcome.manifest.schema_version,
                MATCHING_SCHEMA_VERSION,
            )
            self.assertEqual(outcome.manifest.state, original.state)
            self.assertEqual(outcome.manifest.completed_at, original.completed_at)
            self.assertEqual(
                MATCHING_ARTIFACT_NAMES,
                MATCHING_ARTIFACT_NAMES.intersection(outcome.manifest.artifacts),
            )
            for name, entry in original.artifacts.items():
                self.assertEqual(outcome.manifest.artifacts[name], entry)
                path = run_directory / entry.filename
                self.assertEqual(
                    (path.read_bytes(), path.stat().st_mtime_ns),
                    primary_state[name],
                )

            validated = validate_artifact_set(manifest_path)
            self.assertEqual(len(validated.musicbrainz_album_group_rows), 1)
            self.assertEqual(len(validated.musicbrainz_album_candidate_rows), 2)
            self.assertEqual(
                len(validated.musicbrainz_recording_candidate_rows),
                2,
            )
            self.assertEqual(len(validated.musicbrainz_match_result_rows), 2)

            configurations = []
            timestamps = []
            for logical_name in MATCHING_ARTIFACT_NAMES:
                entry = validated.manifest.artifacts[logical_name]
                report_path = run_directory / entry.filename
                self.assertEqual(
                    entry.sha256,
                    hashlib.sha256(report_path.read_bytes()).hexdigest(),
                )
                with report_path.open(encoding="utf-8", newline="") as report:
                    reader = csv.DictReader(report)
                    rows = list(reader)
                    self.assertEqual(
                        tuple(reader.fieldnames or ()),
                        HEADERS[entry.filename],
                    )
                self.assertEqual(entry.row_count, len(rows))
                configurations.append(entry.configuration)
                timestamps.append(entry.generated_at)

            self.assertTrue(all(value == configurations[0] for value in configurations))
            self.assertEqual(set(timestamps), {"2026-07-05T18:00:01Z"})
            self.assertEqual(
                configurations[0],
                {
                    "client_policy_version": "musicbrainz-client-v1",
                    "scoring_model_version": "musicbrainz-confidence-v1",
                    "candidate_limit": 10,
                    "match_threshold": "85.00",
                    "ambiguous_threshold": "60.00",
                    "margin_threshold": "10.00",
                    "cache_max_age_seconds": 2_592_000,
                    "rate_interval_seconds": 1.1,
                    "retry_count": 3,
                    "timeout_seconds": 30.0,
                    "consent_source": "cli",
                },
            )
            self.assertFalse(any(run_directory.glob(".*.tmp")))

    def test_preserves_unrelated_analysis_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            analysis = analyze_scan_run(run_directory, clock=_clock(2))
            analysis_entries = {
                name: entry
                for name, entry in analysis.manifest.artifacts.items()
                if entry.role == "derived"
            }
            analysis_files = {
                entry.filename: (
                    (run_directory / entry.filename).read_bytes(),
                    (run_directory / entry.filename).stat().st_mtime_ns,
                )
                for entry in analysis_entries.values()
            }

            _register(run_directory, second=3)

            manifest = load_scan_manifest(run_directory / "scan_manifest.json")
            for name, entry in analysis_entries.items():
                self.assertEqual(manifest.artifacts[name], entry)
            for filename, state in analysis_files.items():
                path = run_directory / filename
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), state)
            validate_artifact_set(run_directory / "scan_manifest.json")

    def test_successful_rerun_replaces_only_matching_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            first = _register(run_directory, second=4)
            primary_entries = {
                name: entry
                for name, entry in first.manifest.artifacts.items()
                if name not in MATCHING_ARTIFACT_NAMES
            }

            second = _register(run_directory, second=5, empty=True)

            for name, entry in primary_entries.items():
                self.assertEqual(second.manifest.artifacts[name], entry)
            validated = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(validated.musicbrainz_album_candidate_rows, ())
            self.assertEqual(validated.musicbrainz_recording_candidate_rows, ())
            self.assertEqual(
                {row.reason_code for row in validated.musicbrainz_match_result_rows},
                {"no_candidates"},
            )
            self.assertFalse(any(run_directory.glob(".*.tmp")))

    def test_failed_replacement_restores_previous_complete_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            _register(run_directory, second=6)
            original_manifest = manifest_path.read_bytes()
            original_reports = _matching_bytes(run_directory)
            real_replace = os.replace
            failed = False

            def fail_one_artifact(source: Path, target: Path) -> None:
                nonlocal failed
                if (
                    not failed
                    and Path(target).name == "musicbrainz_recording_candidates.csv"
                ):
                    failed = True
                    raise OSError("injected matching artifact failure")
                real_replace(source, target)

            with (
                mock.patch(
                    "music_manager.musicbrainz_runs.os.replace",
                    side_effect=fail_one_artifact,
                ),
                self.assertRaisesRegex(OSError, "injected matching"),
            ):
                _register(run_directory, second=7, empty=True)

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(_matching_bytes(run_directory), original_reports)
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            validate_artifact_set(manifest_path)

    def test_failed_first_registration_removes_files_and_temps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            original_manifest = manifest_path.read_bytes()
            real_replace = os.replace

            def fail_manifest(source: Path, target: Path) -> None:
                if Path(target) == manifest_path:
                    raise OSError("injected manifest failure")
                real_replace(source, target)

            with (
                mock.patch(
                    "music_manager.musicbrainz_runs.os.replace",
                    side_effect=fail_manifest,
                ),
                self.assertRaisesRegex(OSError, "injected manifest"),
            ):
                _register(run_directory, second=8)

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertFalse(
                any(
                    (run_directory / filename).exists()
                    for filename in MATCHING_FILENAMES
                )
            )
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            validate_artifact_set(manifest_path)

    def test_staging_failure_preserves_manifest_and_cleans_temps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            original_manifest = manifest_path.read_bytes()
            from music_manager import musicbrainz_runs

            real_stage = musicbrainz_runs._stage_csv
            staged_reports = 0

            def fail_second_stage(*args: object, **kwargs: object):
                nonlocal staged_reports
                staged_reports += 1
                if staged_reports == 2:
                    raise OSError("injected matching staging failure")
                return real_stage(*args, **kwargs)

            with (
                mock.patch(
                    "music_manager.musicbrainz_runs._stage_csv",
                    side_effect=fail_second_stage,
                ),
                self.assertRaisesRegex(OSError, "injected matching staging"),
            ):
                _register(run_directory, second=9)

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertFalse(
                any(
                    (run_directory / filename).exists()
                    for filename in MATCHING_FILENAMES
                )
            )
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            validate_artifact_set(manifest_path)

    def test_failed_final_registration_restores_previous_family(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            manifest_path = run_directory / "scan_manifest.json"
            _register(run_directory, second=9)
            original_manifest = manifest_path.read_bytes()
            original_reports = _matching_bytes(run_directory)
            real_replace = os.replace
            manifest_replacements = 0

            def fail_final_manifest(source: Path, target: Path) -> None:
                nonlocal manifest_replacements
                if Path(target) == manifest_path:
                    manifest_replacements += 1
                    if manifest_replacements == 2:
                        raise OSError("injected final registration failure")
                real_replace(source, target)

            with (
                mock.patch(
                    "music_manager.musicbrainz_runs.os.replace",
                    side_effect=fail_final_manifest,
                ),
                self.assertRaisesRegex(OSError, "injected final"),
            ):
                _register(run_directory, second=10, empty=True)

            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(_matching_bytes(run_directory), original_reports)
            self.assertFalse(any(run_directory.glob(".*.tmp")))
            validate_artifact_set(manifest_path)

    def test_csv_bytes_are_deterministic_across_response_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_root = root / "first"
            second_root = root / "second"
            first_root.mkdir()
            second_root.mkdir()
            first = _copy_valid_run(first_root)
            second = _copy_valid_run(second_root)
            _reverse_inventory_rows(second)

            _register(first, second=11)
            _register(second, second=12, reverse=True)

            self.assertEqual(_matching_bytes(first), _matching_bytes(second))
            album_csv = (first / "musicbrainz_album_candidates.csv").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                '"Synthetic, Album?"',
                album_csv,
            )
            self.assertIn(
                '"[""Compilation"",""Soundtrack""]"',
                album_csv,
            )
            recording_csv = (first / "musicbrainz_recording_candidates.csv").read_text(
                encoding="utf-8"
            )
            self.assertIn("1.0000", recording_csv)
            self.assertIn("99.95", recording_csv)

    def test_reported_source_paths_remain_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            library_path = run_directory / "library_scan.csv"
            with library_path.open(encoding="utf-8", newline="") as report:
                reader = csv.DictReader(report)
                rows = list(reader)
                header = tuple(reader.fieldnames or ())
            reported = Path("private/source/Artist/Album/Track.flac")
            rows[0]["path"] = reported.as_posix()
            rows[0]["file_record_id"] = str(
                make_file_record_id(SCAN_ID, reported.as_posix())
            )
            with library_path.open("w", encoding="utf-8", newline="") as report:
                writer = csv.DictWriter(
                    report,
                    fieldnames=header,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(rows)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["library_scan"]["sha256"] = hashlib.sha256(
                library_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            subjects, scoring = _matching_inputs(run_directory)
            original_open = Path.open
            original_stat = Path.stat
            original_resolve = Path.resolve

            def is_reported(path: Path) -> bool:
                return (
                    len(path.parts) >= len(reported.parts)
                    and path.parts[-len(reported.parts) :] == reported.parts
                )

            def guarded_open(path: Path, *args: object, **kwargs: object):
                if is_reported(path):
                    raise AssertionError(f"opened reported path: {path}")
                return original_open(path, *args, **kwargs)

            def guarded_stat(path: Path, *args: object, **kwargs: object):
                if is_reported(path):
                    raise AssertionError(f"statted reported path: {path}")
                return original_stat(path, *args, **kwargs)

            def guarded_resolve(path: Path, *args: object, **kwargs: object):
                if is_reported(path):
                    raise AssertionError(f"resolved reported path: {path}")
                return original_resolve(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", guarded_open),
                mock.patch.object(Path, "stat", guarded_stat),
                mock.patch.object(Path, "resolve", guarded_resolve),
            ):
                register_musicbrainz_artifacts(
                    run_directory,
                    subjects,
                    scoring,
                    consent_source="config",
                    clock=_clock(13),
                )

            validate_artifact_set(manifest_path)


if __name__ == "__main__":
    unittest.main()
