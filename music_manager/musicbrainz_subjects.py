"""Deterministic in-memory MusicBrainz subjects and candidate retrieval."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable
from uuid import UUID, uuid5

from music_manager.artifact_schema import LibraryScanRow, ValidatedArtifactSet
from music_manager.matcher import (
    MUSICBRAINZ_CANDIDATE_LIMIT,
    MusicBrainzClient,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
)


@dataclass(frozen=True)
class AlbumSubject:
    """One deterministic local album group eligible for candidate retrieval."""

    album_group_id: UUID
    query_artist: str
    query_album: str
    normalized_artist: str
    normalized_album: str
    release_year: int | None
    member_file_record_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class RecordingSubject:
    """One readable local recording eligible for candidate retrieval."""

    file_record_id: UUID
    query_artist: str
    query_title: str
    normalized_artist: str
    normalized_title: str
    normalized_album: str
    duration_seconds: Decimal | None
    release_year: int | None


@dataclass(frozen=True)
class IneligibleRecordingSubject:
    """One audio row retained with its stable ineligibility reason."""

    file_record_id: UUID
    reason_code: str


@dataclass(frozen=True)
class MusicBrainzSubjectSet:
    """All deterministic in-memory subjects extracted from one scan."""

    scan_id: UUID
    albums: tuple[AlbumSubject, ...]
    recordings: tuple[RecordingSubject, ...]
    ineligible_recordings: tuple[IneligibleRecordingSubject, ...]


@dataclass(frozen=True)
class AlbumCandidateValues:
    """Client-boundary release-group values for one local album."""

    album_group_id: UUID
    candidates: tuple[ReleaseGroupSearchResult, ...]


@dataclass(frozen=True)
class RecordingCandidateValues:
    """Client-boundary recording values for one local recording."""

    file_record_id: UUID
    candidates: tuple[RecordingSearchResult, ...]


@dataclass(frozen=True)
class MusicBrainzCandidateRetrieval:
    """In-memory candidate values without scores, classes, or artifact rows."""

    scan_id: UUID
    albums: tuple[AlbumCandidateValues, ...]
    recordings: tuple[RecordingCandidateValues, ...]


@dataclass
class _AlbumBucket:
    query_artists: set[str]
    query_albums: set[str]
    release_years: set[int]
    member_ids: set[UUID]


def normalize_musicbrainz_metadata(value: str) -> str:
    """Normalize local identity text according to the v0.4 contract."""
    if not isinstance(value, str):
        raise TypeError("MusicBrainz metadata values must be text")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def musicbrainz_query_text(value: str) -> str:
    """Normalize query display text without changing its case."""
    if not isinstance(value, str):
        raise TypeError("MusicBrainz query values must be text")
    return " ".join(unicodedata.normalize("NFKC", value).split())


def extract_musicbrainz_subjects(
    artifacts: ValidatedArtifactSet,
) -> MusicBrainzSubjectSet:
    """Extract deterministic local subjects without opening reported paths."""
    if not isinstance(artifacts, ValidatedArtifactSet):
        raise TypeError("MusicBrainz subjects require a validated artifact set")

    scan_id = artifacts.manifest.scan_id
    recordings: list[RecordingSubject] = []
    ineligible: list[IneligibleRecordingSubject] = []
    album_buckets: dict[tuple[str, str], _AlbumBucket] = {}

    for row in sorted(
        artifacts.library_rows,
        key=lambda value: str(value.file_record_id),
    ):
        if row.file_type != "audio":
            continue
        if row.scan_id != scan_id:
            raise ValueError("library row scan_id does not match the manifest")

        normalized_artist = normalize_musicbrainz_metadata(row.artist)
        normalized_title = normalize_musicbrainz_metadata(row.title)
        reason_code = _recording_ineligibility(
            row,
            normalized_artist=normalized_artist,
            normalized_title=normalized_title,
        )
        if reason_code is None:
            recordings.append(
                RecordingSubject(
                    file_record_id=row.file_record_id,
                    query_artist=musicbrainz_query_text(row.artist),
                    query_title=musicbrainz_query_text(row.title),
                    normalized_artist=normalized_artist,
                    normalized_title=normalized_title,
                    normalized_album=normalize_musicbrainz_metadata(row.album),
                    duration_seconds=row.duration_seconds,
                    release_year=row.release_year,
                )
            )
        else:
            ineligible.append(
                IneligibleRecordingSubject(
                    file_record_id=row.file_record_id,
                    reason_code=reason_code,
                )
            )

        _add_album_membership(album_buckets, row)

    albums = tuple(
        sorted(
            (
                _album_subject(scan_id, key, bucket)
                for key, bucket in album_buckets.items()
            ),
            key=lambda subject: str(subject.album_group_id),
        )
    )
    return MusicBrainzSubjectSet(
        scan_id=scan_id,
        albums=albums,
        recordings=tuple(recordings),
        ineligible_recordings=tuple(ineligible),
    )


def retrieve_musicbrainz_candidates(
    subjects: MusicBrainzSubjectSet,
    client: MusicBrainzClient,
) -> MusicBrainzCandidateRetrieval:
    """Retrieve boundary values for eligible subjects through an injected client."""
    album_values = tuple(
        AlbumCandidateValues(
            album_group_id=subject.album_group_id,
            candidates=tuple(
                client.search_release_groups(
                    subject.query_artist,
                    subject.query_album,
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )
            ),
        )
        for subject in subjects.albums
    )
    recording_values = tuple(
        RecordingCandidateValues(
            file_record_id=subject.file_record_id,
            candidates=tuple(
                client.search_recordings(
                    subject.query_artist,
                    subject.query_title,
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )
            ),
        )
        for subject in subjects.recordings
    )
    return MusicBrainzCandidateRetrieval(
        scan_id=subjects.scan_id,
        albums=album_values,
        recordings=recording_values,
    )


def _recording_ineligibility(
    row: LibraryScanRow,
    *,
    normalized_artist: str,
    normalized_title: str,
) -> str | None:
    if row.record_status != "ok":
        return "unreadable_record"
    if not normalized_artist:
        return "missing_artist"
    if not normalized_title:
        return "missing_title"
    return None


def _add_album_membership(
    buckets: dict[tuple[str, str], _AlbumBucket],
    row: LibraryScanRow,
) -> None:
    if row.record_status != "ok":
        return
    effective_artist = row.album_artist if row.album_artist else row.artist
    normalized_artist = normalize_musicbrainz_metadata(effective_artist)
    normalized_album = normalize_musicbrainz_metadata(row.album)
    if not normalized_artist or not normalized_album:
        return

    key = (normalized_artist, normalized_album)
    bucket = buckets.setdefault(
        key,
        _AlbumBucket(
            query_artists=set(),
            query_albums=set(),
            release_years=set(),
            member_ids=set(),
        ),
    )
    bucket.query_artists.add(musicbrainz_query_text(effective_artist))
    bucket.query_albums.add(musicbrainz_query_text(row.album))
    if row.release_year is not None:
        bucket.release_years.add(row.release_year)
    bucket.member_ids.add(row.file_record_id)


def _album_subject(
    scan_id: UUID,
    key: tuple[str, str],
    bucket: _AlbumBucket,
) -> AlbumSubject:
    normalized_artist, normalized_album = key
    album_group_id = uuid5(
        scan_id,
        f"musicbrainz-album-group-v1\0{normalized_artist}\0{normalized_album}",
    )
    return AlbumSubject(
        album_group_id=album_group_id,
        query_artist=_query_choice(bucket.query_artists),
        query_album=_query_choice(bucket.query_albums),
        normalized_artist=normalized_artist,
        normalized_album=normalized_album,
        release_year=(
            next(iter(bucket.release_years)) if len(bucket.release_years) == 1 else None
        ),
        member_file_record_ids=tuple(sorted(bucket.member_ids, key=str)),
    )


def _query_choice(values: Iterable[str]) -> str:
    return min(values, key=lambda value: (value.casefold(), value))
