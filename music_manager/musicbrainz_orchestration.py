"""Explicit opt-in orchestration for one MusicBrainz matching run."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable
from uuid import UUID

from music_manager.matcher import (
    MUSICBRAINZ_CANDIDATE_LIMIT,
    MusicBrainzClient,
    MusicBrainzClientFactory,
    MusicBrainzPreflight,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
    open_musicbrainz_client_boundary,
)
from music_manager.musicbrainz_client import (
    MusicBrainzClientError,
    MusicBrainzResponseError,
    ProductionMusicBrainzClient,
)
from music_manager.musicbrainz_runs import (
    MusicBrainzArtifactOutcome,
    register_musicbrainz_artifacts,
)
from music_manager.musicbrainz_scoring import (
    MusicBrainzMatchResult,
    MusicBrainzScoringResult,
    musicbrainz_error_result,
    score_musicbrainz_candidates,
)
from music_manager.musicbrainz_subjects import (
    MusicBrainzCandidateRetrieval,
    MusicBrainzSubjectSet,
    extract_musicbrainz_subjects,
    retrieve_musicbrainz_candidates,
)


MUSICBRAINZ_OUTPUT_FILES = (
    "musicbrainz_album_groups.csv",
    "musicbrainz_album_candidates.csv",
    "musicbrainz_recording_candidates.csv",
    "musicbrainz_match_results.csv",
)

PreRequestHook = Callable[[MusicBrainzPreflight], None]


@dataclass(frozen=True)
class MusicBrainzMatchSummary:
    """Allowlisted terminal counts for a completed matching run."""

    album_groups: int
    recordings: int
    ineligible_recordings: int
    candidates: int
    matched: int
    ambiguous: int
    unmatched: int
    not_eligible: int
    errors: int
    malformed_items: int
    output_files: tuple[str, ...]


@dataclass(frozen=True)
class MusicBrainzMatchOutcome:
    """One completed and atomically registered matching run."""

    artifacts: MusicBrainzArtifactOutcome
    subjects: MusicBrainzSubjectSet
    scoring: MusicBrainzScoringResult
    summary: MusicBrainzMatchSummary
    consent_source: str


class _ErrorCapturingClient:
    """Continue retrieval while retaining sanitized per-subject failures."""

    def __init__(
        self,
        subjects: MusicBrainzSubjectSet,
        client: MusicBrainzClient,
    ) -> None:
        self._subjects = subjects
        self._client = client
        self._album_index = 0
        self._recording_index = 0
        self.errors: dict[tuple[str, UUID], MusicBrainzMatchResult] = {}

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple[ReleaseGroupSearchResult, ...]:
        subject = self._subjects.albums[self._album_index]
        self._album_index += 1
        if (
            album_artist != subject.query_artist
            or album_title != subject.query_album
            or limit != MUSICBRAINZ_CANDIDATE_LIMIT
        ):
            raise AssertionError("album retrieval order or query changed")
        try:
            return tuple(
                self._client.search_release_groups(
                    album_artist,
                    album_title,
                    limit,
                )
            )
        except (MusicBrainzClientError, TypeError, ValueError) as error:
            self.errors[("album", subject.album_group_id)] = musicbrainz_error_result(
                self._subjects.scan_id,
                "album",
                subject.album_group_id,
                _failure_reason(error),
            )
            return ()

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple[RecordingSearchResult, ...]:
        subject = self._subjects.recordings[self._recording_index]
        self._recording_index += 1
        if (
            track_artist != subject.query_artist
            or track_title != subject.query_title
            or limit != MUSICBRAINZ_CANDIDATE_LIMIT
        ):
            raise AssertionError("recording retrieval order or query changed")
        try:
            return tuple(
                self._client.search_recordings(
                    track_artist,
                    track_title,
                    limit,
                )
            )
        except (MusicBrainzClientError, TypeError, ValueError) as error:
            self.errors[("recording", subject.file_record_id)] = (
                musicbrainz_error_result(
                    self._subjects.scan_id,
                    "recording",
                    subject.file_record_id,
                    _failure_reason(error),
                )
            )
            return ()


def run_musicbrainz_match(
    run_directory: Path,
    *,
    enabled: bool,
    consent_source: str,
    client_factory: MusicBrainzClientFactory | None = None,
    on_pre_request: PreRequestHook | None = None,
) -> MusicBrainzMatchOutcome:
    """Run and register the v0.4 pipeline after explicit consent preflight."""
    factory = ProductionMusicBrainzClient if client_factory is None else client_factory
    boundary = open_musicbrainz_client_boundary(
        run_directory,
        enabled=enabled,
        consent_source=consent_source,
        client_factory=factory,
    )
    preflight = boundary.preflight
    client = boundary.client
    try:
        if on_pre_request is not None:
            on_pre_request(preflight)
        subjects = extract_musicbrainz_subjects(preflight.artifacts)
        retrieval, errors = _retrieve_candidates(subjects, client)
        scoring = _score_with_errors(subjects, retrieval, errors)
        artifacts = register_musicbrainz_artifacts(
            preflight.run_directory,
            subjects,
            scoring,
            consent_source=preflight.consent_source,
        )
        return MusicBrainzMatchOutcome(
            artifacts=artifacts,
            subjects=subjects,
            scoring=scoring,
            summary=_summarize(subjects, scoring, client),
            consent_source=preflight.consent_source,
        )
    finally:
        _close_client(client)


def _retrieve_candidates(
    subjects: MusicBrainzSubjectSet,
    client: MusicBrainzClient,
) -> tuple[
    MusicBrainzCandidateRetrieval,
    dict[tuple[str, UUID], MusicBrainzMatchResult],
]:
    capturing_client = _ErrorCapturingClient(subjects, client)
    retrieval = retrieve_musicbrainz_candidates(subjects, capturing_client)
    return retrieval, capturing_client.errors


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, (MusicBrainzResponseError, TypeError, ValueError)):
        return "malformed_response"
    return "request_failed"


def _score_with_errors(
    subjects: MusicBrainzSubjectSet,
    retrieval: MusicBrainzCandidateRetrieval,
    errors: dict[tuple[str, UUID], MusicBrainzMatchResult],
) -> MusicBrainzScoringResult:
    scoring = score_musicbrainz_candidates(subjects, retrieval)
    if not errors:
        return scoring
    return replace(
        scoring,
        match_results=tuple(
            errors.get((result.subject_type, result.subject_id), result)
            for result in scoring.match_results
        ),
    )


def _summarize(
    subjects: MusicBrainzSubjectSet,
    scoring: MusicBrainzScoringResult,
    client: MusicBrainzClient,
) -> MusicBrainzMatchSummary:
    statuses = Counter(result.status for result in scoring.match_results)
    malformed_items = getattr(client, "malformed_item_count", 0)
    if (
        isinstance(malformed_items, bool)
        or not isinstance(malformed_items, int)
        or malformed_items < 0
    ):
        malformed_items = 0
    return MusicBrainzMatchSummary(
        album_groups=len(subjects.albums),
        recordings=len(subjects.recordings),
        ineligible_recordings=len(subjects.ineligible_recordings),
        candidates=(len(scoring.album_candidates) + len(scoring.recording_candidates)),
        matched=statuses["matched"],
        ambiguous=statuses["ambiguous"],
        unmatched=statuses["unmatched"],
        not_eligible=statuses["not_eligible"],
        errors=statuses["error"],
        malformed_items=malformed_items,
        output_files=MUSICBRAINZ_OUTPUT_FILES,
    )


def _close_client(client: MusicBrainzClient) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()
