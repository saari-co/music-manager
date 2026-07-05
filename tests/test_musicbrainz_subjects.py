"""Offline tests for deterministic MusicBrainz subjects and retrieval."""

from __future__ import annotations

import socket
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid5

from music_manager.artifact_schema import (
    LibraryScanRow,
    ValidatedArtifactSet,
    make_file_record_id,
    validate_artifact_set,
)
from music_manager.matcher import (
    MUSICBRAINZ_CANDIDATE_LIMIT,
    MusicBrainzClient,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
)
from music_manager.musicbrainz_subjects import (
    MusicBrainzSubjectSet,
    extract_musicbrainz_subjects,
    musicbrainz_query_text,
    normalize_musicbrainz_metadata,
    retrieve_musicbrainz_candidates,
)


FIXTURES = Path(__file__).parent / "fixtures" / "v0_3" / "valid"
VALIDATED = validate_artifact_set(FIXTURES / "scan_manifest.json")
SCAN_ID = VALIDATED.manifest.scan_id
_AUDIO_TEMPLATE = VALIDATED.library_rows[0].to_csv_row()
_ARCHIVE_TEMPLATE = VALIDATED.library_rows[1].to_csv_row()


def _row(
    path: str,
    *,
    artist: str = "Track Artist",
    album_artist: str = "",
    title: str = "Track Title",
    album: str = "Album Title",
    release_year: int | None = 2031,
    record_status: str = "ok",
    file_type: str = "audio",
    genre: str = "",
    composer: str = "",
    codec: str = "FLAC",
) -> LibraryScanRow:
    source = (
        _AUDIO_TEMPLATE.copy() if file_type == "audio" else _ARCHIVE_TEMPLATE.copy()
    )
    source.update(
        {
            "scan_id": str(SCAN_ID),
            "file_record_id": str(make_file_record_id(SCAN_ID, path)),
            "path": path,
            "artist": artist,
            "album_artist": album_artist,
            "title": title,
            "album": album,
            "release_year": "" if release_year is None else str(release_year),
            "record_status": record_status,
            "genre": genre,
            "composer": composer,
            "codec": codec,
        }
    )
    return LibraryScanRow.from_csv_row(source)


def _artifacts(*rows: LibraryScanRow) -> ValidatedArtifactSet:
    return replace(VALIDATED, library_rows=tuple(rows))


class _FakeClient:
    def __init__(self) -> None:
        self.release_group_calls: list[tuple[str, str, int]] = []
        self.recording_calls: list[tuple[str, str, int]] = []

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple[ReleaseGroupSearchResult, ...]:
        self.release_group_calls.append((album_artist, album_title, limit))
        return (
            ReleaseGroupSearchResult(
                mbid=UUID("11111111-1111-4111-8111-111111111111"),
                title=f"Candidate for {album_title}",
                artist_credit=album_artist,
                search_score=90,
            ),
        )

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple[RecordingSearchResult, ...]:
        self.recording_calls.append((track_artist, track_title, limit))
        return (
            RecordingSearchResult(
                mbid=UUID("22222222-2222-4222-8222-222222222222"),
                title=f"Candidate for {track_title}",
                artist_credit=track_artist,
                search_score=80,
            ),
        )


def _subject_rows() -> tuple[LibraryScanRow, ...]:
    return (
        _row(
            "library/a.flac",
            artist="First Track Artist",
            album_artist="Velvet  Meridian",
            title="First  Signal",
            album="Paper  Constellations",
            release_year=2031,
        ),
        _row(
            "library/b.flac",
            artist="Second Track Artist",
            album_artist="velvet meridian",
            title="Second Signal",
            album="paper constellations",
            release_year=2032,
        ),
        _row(
            "library/c.flac",
            artist="Solo Artist",
            title="Solo Signal",
            album="Paper Constellations",
            release_year=2030,
        ),
        _row(
            "library/d.flac",
            artist="Track Artist",
            album_artist="Velvet Meridian",
            title="",
            album="Paper Constellations",
            release_year=2031,
        ),
        _row(
            "library/e.flac",
            artist="",
            album_artist="Velvet Meridian",
            title="Compilation Signal",
            album="Paper Constellations",
            release_year=2031,
        ),
        _row(
            "library/f.flac",
            artist="",
            title="",
            album="Paper Constellations",
            record_status="error",
        ),
        _row(
            "library/g.flac",
            artist="No Album Artist",
            title="Loose Signal",
            album="",
            release_year=None,
        ),
        _row(
            "library/archive.zip",
            file_type="archive",
            artist="Archive Artist",
            title="Archive Title",
            album="Archive Album",
        ),
    )


class MusicBrainzSubjectExtractionTests(unittest.TestCase):
    def test_normalization_matches_metadata_and_query_contracts(self) -> None:
        self.assertEqual(
            normalize_musicbrainz_metadata("Straße  Ａlbum: Part II"),
            "strasse album: part ii",
        )
        self.assertEqual(
            musicbrainz_query_text("Straße  Ａlbum: Part II"),
            "Straße Album: Part II",
        )

    def test_recording_eligibility_and_album_grouping_follow_contract(self) -> None:
        rows = _subject_rows()

        subjects = extract_musicbrainz_subjects(_artifacts(*rows))

        self.assertEqual(subjects.scan_id, SCAN_ID)
        self.assertEqual(len(subjects.recordings), 4)
        self.assertEqual(
            {subject.file_record_id for subject in subjects.recordings},
            {rows[index].file_record_id for index in (0, 1, 2, 6)},
        )
        recording = next(
            value
            for value in subjects.recordings
            if value.file_record_id == rows[0].file_record_id
        )
        self.assertEqual(recording.query_artist, "First Track Artist")
        self.assertEqual(recording.query_title, "First Signal")
        self.assertEqual(recording.normalized_artist, "first track artist")
        self.assertEqual(recording.normalized_title, "first signal")
        self.assertEqual(recording.normalized_album, "paper constellations")

        self.assertEqual(
            {
                value.file_record_id: value.reason_code
                for value in subjects.ineligible_recordings
            },
            {
                rows[3].file_record_id: "missing_title",
                rows[4].file_record_id: "missing_artist",
                rows[5].file_record_id: "unreadable_record",
            },
        )

        self.assertEqual(len(subjects.albums), 2)
        grouped = {
            (album.normalized_artist, album.normalized_album): album
            for album in subjects.albums
        }
        velvet = grouped[("velvet meridian", "paper constellations")]
        self.assertEqual(velvet.query_artist, "Velvet Meridian")
        self.assertEqual(velvet.query_album, "Paper Constellations")
        self.assertIsNone(velvet.release_year)
        self.assertEqual(
            velvet.member_file_record_ids,
            tuple(
                sorted(
                    {rows[index].file_record_id for index in (0, 1, 3, 4)},
                    key=str,
                )
            ),
        )
        self.assertEqual(
            velvet.album_group_id,
            uuid5(
                SCAN_ID,
                "musicbrainz-album-group-v1\0velvet meridian\0paper constellations",
            ),
        )

        solo = grouped[("solo artist", "paper constellations")]
        self.assertEqual(solo.query_artist, "Solo Artist")
        self.assertEqual(solo.release_year, 2030)
        self.assertEqual(solo.member_file_record_ids, (rows[2].file_record_id,))

    def test_subjects_are_identical_after_input_row_reordering(self) -> None:
        rows = _subject_rows()

        forward = extract_musicbrainz_subjects(_artifacts(*rows))
        reversed_rows = extract_musicbrainz_subjects(_artifacts(*reversed(rows)))

        self.assertEqual(forward, reversed_rows)
        self.assertEqual(
            tuple(str(value.album_group_id) for value in forward.albums),
            tuple(sorted(str(value.album_group_id) for value in forward.albums)),
        )
        self.assertEqual(
            tuple(str(value.file_record_id) for value in forward.recordings),
            tuple(sorted(str(value.file_record_id) for value in forward.recordings)),
        )

    def test_extractor_requires_validated_artifact_container(self) -> None:
        with self.assertRaises(TypeError):
            extract_musicbrainz_subjects(())  # type: ignore[arg-type]


class MusicBrainzCandidateRetrievalTests(unittest.TestCase):
    def test_only_eligible_subjects_call_injected_client(self) -> None:
        subjects = extract_musicbrainz_subjects(_artifacts(*_subject_rows()))
        client = _FakeClient()
        self.assertIsInstance(client, MusicBrainzClient)

        retrieved = retrieve_musicbrainz_candidates(subjects, client)

        self.assertEqual(retrieved.scan_id, SCAN_ID)
        self.assertEqual(
            client.release_group_calls,
            [
                (
                    subject.query_artist,
                    subject.query_album,
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )
                for subject in subjects.albums
            ],
        )
        self.assertEqual(
            client.recording_calls,
            [
                (
                    subject.query_artist,
                    subject.query_title,
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )
                for subject in subjects.recordings
            ],
        )
        self.assertEqual(
            tuple(value.album_group_id for value in retrieved.albums),
            tuple(value.album_group_id for value in subjects.albums),
        )
        self.assertEqual(
            tuple(value.file_record_id for value in retrieved.recordings),
            tuple(value.file_record_id for value in subjects.recordings),
        )
        self.assertTrue(
            all(
                isinstance(value.candidates[0], ReleaseGroupSearchResult)
                for value in retrieved.albums
            )
        )
        self.assertTrue(
            all(
                isinstance(value.candidates[0], RecordingSearchResult)
                for value in retrieved.recordings
            )
        )

    def test_paths_and_private_metadata_never_reach_client_or_filesystem(self) -> None:
        row = _row(
            "private-user/private-host/01-secret-file.flac",
            artist="Allowlisted Artist",
            album_artist="Allowlisted Album Artist",
            title="Allowlisted Title",
            album="Allowlisted Album",
            genre="private-checksum",
            composer="private-config-path",
            codec="private-audio-data",
        )
        artifacts = _artifacts(row)
        client = _FakeClient()
        private_values = (
            row.path,
            Path(row.path).name,
            str(row.scan_id),
            str(row.file_record_id),
            row.file_fingerprint,
            "private-checksum",
            "private-config-path",
            "private-user",
            "private-host",
            "private-audio-data",
        )

        with (
            mock.patch.object(
                Path,
                "open",
                side_effect=AssertionError("source path opened"),
            ),
            mock.patch.object(
                Path,
                "resolve",
                side_effect=AssertionError("source path resolved"),
            ),
            mock.patch.object(
                Path,
                "stat",
                side_effect=AssertionError("source path statted"),
            ),
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
                side_effect=AssertionError("sleep accessed"),
            ),
        ):
            subjects = extract_musicbrainz_subjects(artifacts)
            retrieved = retrieve_musicbrainz_candidates(subjects, client)

        calls = repr((client.release_group_calls, client.recording_calls))
        for value in (
            "Allowlisted Artist",
            "Allowlisted Album Artist",
            "Allowlisted Title",
            "Allowlisted Album",
        ):
            self.assertIn(value, calls)
        for value in private_values:
            with self.subTest(value=value):
                self.assertNotIn(value, calls)
        self.assertEqual(
            subjects.recordings[0].file_record_id,
            row.file_record_id,
        )
        self.assertEqual(
            retrieved.recordings[0].file_record_id,
            row.file_record_id,
        )
        self.assertNotIn(row.path, repr(subjects))
        self.assertNotIn(row.file_fingerprint, repr(subjects))

    def test_empty_subject_set_performs_no_client_calls(self) -> None:
        subjects = MusicBrainzSubjectSet(
            scan_id=SCAN_ID,
            albums=(),
            recordings=(),
            ineligible_recordings=(),
        )
        client = _FakeClient()

        retrieved = retrieve_musicbrainz_candidates(subjects, client)

        self.assertEqual(retrieved.albums, ())
        self.assertEqual(retrieved.recordings, ())
        self.assertEqual(client.release_group_calls, [])
        self.assertEqual(client.recording_calls, [])


if __name__ == "__main__":
    unittest.main()
