"""Fully offline tests for the opt-in MusicBrainz client boundary."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_manager.artifact_schema import UnsupportedSchemaVersionError
from music_manager.matcher import (
    MUSICBRAINZ_CONTACT_URL,
    MusicBrainzClient,
    MusicBrainzConsentRequired,
    build_musicbrainz_user_agent,
    open_musicbrainz_client_boundary,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
SCAN_ID = "12345678-1234-4abc-8def-1234567890ab"


class _FakeClient:
    def __init__(self) -> None:
        self.release_group_calls: list[tuple[str, str, int]] = []
        self.recording_calls: list[tuple[str, str, int]] = []

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple:
        self.release_group_calls.append((album_artist, album_title, limit))
        return ()

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple:
        self.recording_calls.append((track_artist, track_title, limit))
        return ()


class MusicBrainzBoundaryTests(unittest.TestCase):
    def test_user_agent_is_identifiable_and_stable(self) -> None:
        self.assertEqual(
            build_musicbrainz_user_agent("0.4.0"),
            f"music-manager/0.4.0 ({MUSICBRAINZ_CONTACT_URL})",
        )
        for invalid in ("", " 0.4.0", "0.4.0 local"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    build_musicbrainz_user_agent(invalid)

    def test_fake_implements_the_client_protocol(self) -> None:
        self.assertIsInstance(_FakeClient(), MusicBrainzClient)

    def test_default_opt_out_prevents_artifact_and_client_access(self) -> None:
        client_factory = mock.Mock(side_effect=AssertionError("client accessed"))
        with (
            mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("artifact accessed"),
            ),
            self.assertRaises(MusicBrainzConsentRequired),
        ):
            open_musicbrainz_client_boundary(
                Path("/private/nonexistent/reports/run"),
                enabled=False,
                consent_source="default",
                client_factory=client_factory,
            )
        client_factory.assert_not_called()

    def test_legacy_input_fails_before_client_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            legacy_report = Path(temporary_directory) / "library_scan.csv"
            legacy_report.write_text("path,title\nTrack.mp3,Title\n", encoding="utf-8")
            client_factory = mock.Mock(side_effect=AssertionError("client accessed"))

            with self.assertRaisesRegex(ValueError, "does not exist"):
                open_musicbrainz_client_boundary(
                    legacy_report,
                    enabled=True,
                    consent_source="cli",
                    client_factory=client_factory,
                )

            client_factory.assert_not_called()

    def test_unsupported_schema_fails_before_client_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / SCAN_ID
            shutil.copytree(FIXTURES, run_directory)
            manifest_path = run_directory / "scan_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = "1.3.0"
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )
            client_factory = mock.Mock(side_effect=AssertionError("client accessed"))

            with self.assertRaises(UnsupportedSchemaVersionError):
                open_musicbrainz_client_boundary(
                    run_directory,
                    enabled=True,
                    consent_source="cli",
                    client_factory=client_factory,
                )

            client_factory.assert_not_called()

    def test_valid_preflight_opens_only_registered_artifacts_then_factory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory) / SCAN_ID
            shutil.copytree(FIXTURES, run_directory)
            allowed = {
                run_directory / "scan_manifest.json",
                run_directory / "library_scan.csv",
                run_directory / "scan_errors.csv",
            }
            opened: list[Path] = []
            original_open = Path.open

            def guarded_open(path: Path, *args: object, **kwargs: object):
                opened.append(path)
                if path not in allowed:
                    raise AssertionError(f"unexpected path access: {path}")
                return original_open(path, *args, **kwargs)

            client = _FakeClient()
            client_factory = mock.Mock(return_value=client)
            with mock.patch.object(Path, "open", guarded_open):
                boundary = open_musicbrainz_client_boundary(
                    run_directory,
                    enabled=True,
                    consent_source="cli",
                    client_factory=client_factory,
                )

            self.assertEqual(set(opened), allowed)
            self.assertNotIn(
                run_directory / "Artist/Album/01 Track.flac",
                opened,
            )
            client_factory.assert_called_once_with(
                user_agent=build_musicbrainz_user_agent()
            )
            self.assertIs(boundary.client, client)
            self.assertEqual(client.release_group_calls, [])
            self.assertEqual(client.recording_calls, [])


if __name__ == "__main__":
    unittest.main()
