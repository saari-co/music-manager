"""Fully offline tests for explicit opt-in MusicBrainz orchestration."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

from music_manager.artifact_schema import (
    MATCHING_ARTIFACT_NAMES,
    MATCHING_SCHEMA_VERSION,
    make_file_record_id,
    validate_artifact_set,
)
from music_manager.matcher import (
    MusicBrainzConsentRequired,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
)
from music_manager.musicbrainz_client import (
    MusicBrainzRequestError,
    MusicBrainzResponseError,
)
from music_manager.musicbrainz_orchestration import run_musicbrainz_match


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
PRIVATE_PATH = "private-user/private-host/01-secret-file.flac"
PRIVATE_VALUES = (
    PRIVATE_PATH,
    "01-secret-file.flac",
    "private-user",
    "private-host",
    "private-cache-path",
    "private-audio-data",
)


def _copy_valid_run(root: Path, *, private_path: bool = False) -> Path:
    run_directory = root / str(SCAN_ID)
    shutil.copytree(FIXTURES, run_directory)
    if private_path:
        report_path = run_directory / "library_scan.csv"
        with report_path.open(encoding="utf-8", newline="") as report:
            reader = csv.DictReader(report)
            rows = list(reader)
            fieldnames = tuple(reader.fieldnames or ())
        rows[0]["path"] = PRIVATE_PATH
        rows[0]["file_record_id"] = str(make_file_record_id(SCAN_ID, PRIVATE_PATH))
        with report_path.open("w", encoding="utf-8", newline="") as report:
            writer = csv.DictWriter(
                report,
                fieldnames=fieldnames,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        manifest_path = run_directory / "scan_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["library_scan"]["sha256"] = hashlib.sha256(
            report_path.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
    return run_directory


def _album_candidate() -> ReleaseGroupSearchResult:
    return ReleaseGroupSearchResult(
        mbid=UUID("11111111-1111-4111-8111-111111111111"),
        title="Synthetic Album",
        artist_credit="Synthetic Album Artist",
        first_release_date="2024",
        search_score=100,
    )


def _recording_candidate() -> RecordingSearchResult:
    return RecordingSearchResult(
        mbid=UUID("22222222-2222-4222-8222-222222222222"),
        title="Synthetic Track",
        artist_credit="Synthetic Artist",
        duration_ms=201_250,
        releases=(
            (
                UUID("33333333-3333-4333-8333-333333333333"),
                "Synthetic Album",
            ),
        ),
        search_score=100,
    )


class _FakeClient:
    def __init__(
        self,
        *,
        album_result: object = None,
        recording_result: object = None,
        malformed_item_count: int = 0,
    ) -> None:
        self.album_result = (
            (_album_candidate(),) if album_result is None else album_result
        )
        self.recording_result = (
            (_recording_candidate(),) if recording_result is None else recording_result
        )
        self.malformed_item_count = malformed_item_count
        self.calls: list[tuple[str, str, str, int]] = []
        self.closed = False

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple[ReleaseGroupSearchResult, ...]:
        self.calls.append(("album", album_artist, album_title, limit))
        if isinstance(self.album_result, BaseException):
            raise self.album_result
        return tuple(self.album_result)

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple[RecordingSearchResult, ...]:
        self.calls.append(("recording", track_artist, track_title, limit))
        if isinstance(self.recording_result, BaseException):
            raise self.recording_result
        return tuple(self.recording_result)

    def close(self) -> None:
        self.closed = True


class _FakeFactory:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.user_agents: list[str] = []

    def __call__(self, *, user_agent: str) -> _FakeClient:
        self.user_agents.append(user_agent)
        return self.client


class MusicBrainzOrchestrationTests(unittest.TestCase):
    def test_opt_out_stops_before_artifact_client_or_registration_access(self) -> None:
        factory = mock.Mock(side_effect=AssertionError("client accessed"))
        with (
            mock.patch(
                "music_manager.matcher.validate_artifact_set",
                side_effect=AssertionError("scan artifact accessed"),
            ) as validate,
            mock.patch(
                "music_manager.musicbrainz_orchestration."
                "register_musicbrainz_artifacts",
                side_effect=AssertionError("matching artifact accessed"),
            ) as register,
            self.assertRaises(MusicBrainzConsentRequired),
        ):
            run_musicbrainz_match(
                Path("/private/nonexistent/run"),
                enabled=False,
                consent_source="default",
                client_factory=factory,
            )

        validate.assert_not_called()
        factory.assert_not_called()
        register.assert_not_called()

    def test_invalid_scan_fails_before_client_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            (run_directory / "library_scan.csv").write_text(
                "invalid\n",
                encoding="utf-8",
            )
            factory = mock.Mock(side_effect=AssertionError("client accessed"))

            with self.assertRaises(ValueError):
                run_musicbrainz_match(
                    run_directory,
                    enabled=True,
                    consent_source="cli",
                    client_factory=factory,
                )

            factory.assert_not_called()

    def test_full_pipeline_registers_four_artifacts_and_closes_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            client = _FakeClient(malformed_item_count=2)
            factory = _FakeFactory(client)
            preflight_events: list[tuple[str, int]] = []

            outcome = run_musicbrainz_match(
                run_directory,
                enabled=True,
                consent_source="cli",
                client_factory=factory,
                on_pre_request=lambda preflight: preflight_events.append(
                    (preflight.consent_source, len(client.calls))
                ),
            )

            self.assertEqual(preflight_events, [("cli", 0)])
            self.assertEqual(len(factory.user_agents), 1)
            self.assertTrue(client.closed)
            self.assertEqual(
                [call[0] for call in client.calls],
                ["album", "recording"],
            )
            self.assertEqual(outcome.summary.album_groups, 1)
            self.assertEqual(outcome.summary.recordings, 1)
            self.assertEqual(outcome.summary.candidates, 2)
            self.assertEqual(outcome.summary.matched, 2)
            self.assertEqual(outcome.summary.errors, 0)
            self.assertEqual(outcome.summary.malformed_items, 2)
            validated = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(
                validated.manifest.schema_version,
                MATCHING_SCHEMA_VERSION,
            )
            self.assertEqual(
                MATCHING_ARTIFACT_NAMES,
                MATCHING_ARTIFACT_NAMES.intersection(validated.manifest.artifacts),
            )

    def test_ineligible_recordings_are_reported_without_search_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(Path(temporary_directory))
            report_path = run_directory / "library_scan.csv"
            with report_path.open(encoding="utf-8", newline="") as report:
                reader = csv.DictReader(report)
                rows = list(reader)
                fieldnames = tuple(reader.fieldnames or ())
            rows[0]["artist"] = ""
            rows[0]["album_artist"] = ""
            with report_path.open("w", encoding="utf-8", newline="") as report:
                writer = csv.DictWriter(
                    report,
                    fieldnames=fieldnames,
                    lineterminator="\n",
                )
                writer.writeheader()
                writer.writerows(rows)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"]["library_scan"]["sha256"] = hashlib.sha256(
                report_path.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            client = _FakeClient()

            outcome = run_musicbrainz_match(
                run_directory,
                enabled=True,
                consent_source="cli",
                client_factory=_FakeFactory(client),
            )

            self.assertEqual(client.calls, [])
            self.assertEqual(outcome.summary.album_groups, 0)
            self.assertEqual(outcome.summary.recordings, 0)
            self.assertEqual(outcome.summary.ineligible_recordings, 1)
            self.assertEqual(outcome.summary.not_eligible, 1)
            self.assertEqual(outcome.summary.candidates, 0)
            self.assertEqual(outcome.summary.errors, 0)

    def test_request_and_malformed_failures_register_error_rows_and_continue(
        self,
    ) -> None:
        cases = (
            (
                MusicBrainzRequestError(
                    "private query and response body from private-host"
                ),
                "request_failed",
            ),
            (
                MusicBrainzResponseError(
                    "private query and response body from private-host"
                ),
                "malformed_response",
            ),
        )
        for failure, reason_code in cases:
            with (
                self.subTest(reason_code=reason_code),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                run_directory = _copy_valid_run(Path(temporary_directory))
                client = _FakeClient(album_result=failure)

                outcome = run_musicbrainz_match(
                    run_directory,
                    enabled=True,
                    consent_source="config",
                    client_factory=_FakeFactory(client),
                )

                self.assertTrue(client.closed)
                self.assertEqual(
                    [call[0] for call in client.calls],
                    ["album", "recording"],
                )
                self.assertEqual(outcome.summary.errors, 1)
                results = {
                    (result.subject_type, result.reason_code)
                    for result in outcome.scoring.match_results
                }
                self.assertIn(("album", reason_code), results)
                self.assertIn(("recording", "high_confidence_with_margin"), results)
                report_text = (
                    run_directory / "musicbrainz_match_results.csv"
                ).read_text(encoding="utf-8")
                self.assertIn(f"error,0,,,,{reason_code}", report_text)
                for private_value in PRIVATE_VALUES:
                    self.assertNotIn(private_value, report_text)

    def test_matching_never_accesses_source_paths_network_or_real_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = _copy_valid_run(
                Path(temporary_directory),
                private_path=True,
            )
            client = _FakeClient()
            original_open = Path.open
            original_resolve = Path.resolve
            original_stat = Path.stat

            def guard(path: Path) -> None:
                if PRIVATE_PATH in path.as_posix():
                    raise AssertionError("source-library path accessed")

            def guarded_open(path: Path, *args: object, **kwargs: object):
                guard(path)
                return original_open(path, *args, **kwargs)

            def guarded_resolve(
                path: Path,
                *args: object,
                **kwargs: object,
            ) -> Path:
                guard(path)
                return original_resolve(path, *args, **kwargs)

            def guarded_stat(path: Path, *args: object, **kwargs: object):
                guard(path)
                return original_stat(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", guarded_open),
                mock.patch.object(Path, "resolve", guarded_resolve),
                mock.patch.object(Path, "stat", guarded_stat),
                mock.patch.object(
                    socket,
                    "getaddrinfo",
                    side_effect=AssertionError("DNS accessed"),
                ),
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("socket accessed"),
                ),
                mock.patch.object(
                    time,
                    "sleep",
                    side_effect=AssertionError("real sleep used"),
                ),
            ):
                outcome = run_musicbrainz_match(
                    run_directory,
                    enabled=True,
                    consent_source="cli",
                    client_factory=_FakeFactory(client),
                )

            self.assertEqual(outcome.summary.errors, 0)
            calls = repr(client.calls)
            for private_value in PRIVATE_VALUES:
                self.assertNotIn(private_value, calls)


if __name__ == "__main__":
    unittest.main()
