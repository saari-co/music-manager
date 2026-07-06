"""Deterministic in-memory MusicBrainz evidence scoring and classification."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from music_manager.matcher import RecordingSearchResult, ReleaseGroupSearchResult
from music_manager.musicbrainz_subjects import (
    AlbumCandidateValues,
    AlbumSubject,
    MusicBrainzCandidateRetrieval,
    MusicBrainzSubjectSet,
    RecordingCandidateValues,
    RecordingSubject,
    normalize_musicbrainz_metadata,
)


MUSICBRAINZ_SCORING_MODEL = "musicbrainz-confidence-v1"
MUSICBRAINZ_MATCH_THRESHOLD = Decimal("85.00")
MUSICBRAINZ_AMBIGUOUS_THRESHOLD = Decimal("60.00")
MUSICBRAINZ_MARGIN_THRESHOLD = Decimal("10.00")

_FACTOR_QUANTUM = Decimal("0.0001")
_SCORE_QUANTUM = Decimal("0.01")
_ZERO_FACTOR = Decimal("0.0000")
_ONE_FACTOR = Decimal("1.0000")
_ZERO_SCORE = Decimal("0.00")
_SUBJECT_TYPES = frozenset({"album", "recording"})
_FAILURE_REASONS = frozenset({"request_failed", "malformed_response"})


@dataclass(frozen=True)
class ScoredAlbumCandidate:
    """One ranked release-group candidate with serialized scoring evidence."""

    scan_id: UUID
    album_group_id: UUID
    candidate_rank: int
    candidate: ReleaseGroupSearchResult
    title_similarity: Decimal
    artist_similarity: Decimal
    year_similarity: Decimal
    confidence_score: Decimal


@dataclass(frozen=True)
class ScoredRecordingCandidate:
    """One ranked recording candidate with serialized scoring evidence."""

    scan_id: UUID
    file_record_id: UUID
    candidate_rank: int
    candidate: RecordingSearchResult
    matched_release_mbid: UUID | None
    matched_release_title: str
    title_similarity: Decimal
    artist_similarity: Decimal
    duration_similarity: Decimal
    album_similarity: Decimal
    confidence_score: Decimal


@dataclass(frozen=True)
class MusicBrainzMatchResult:
    """One deterministic in-memory classification for a local subject."""

    scan_id: UUID
    subject_type: str
    subject_id: UUID
    status: str
    candidate_count: int
    top_candidate_mbid: UUID | None
    top_confidence_score: Decimal | None
    confidence_margin: Decimal | None
    reason_code: str


@dataclass(frozen=True)
class MusicBrainzScoringResult:
    """All in-memory scores and classifications for one scan."""

    scan_id: UUID
    album_candidates: tuple[ScoredAlbumCandidate, ...]
    recording_candidates: tuple[ScoredRecordingCandidate, ...]
    match_results: tuple[MusicBrainzMatchResult, ...]


def musicbrainz_text_similarity(left: str, right: str) -> Decimal:
    """Return contract-normalized Levenshtein similarity at four places."""
    normalized_left = normalize_musicbrainz_metadata(left)
    normalized_right = normalize_musicbrainz_metadata(right)
    if not normalized_left or not normalized_right:
        return _ZERO_FACTOR
    if normalized_left == normalized_right:
        return _ONE_FACTOR

    maximum_length = max(len(normalized_left), len(normalized_right))
    distance = _levenshtein_distance(normalized_left, normalized_right)
    similarity = Decimal(maximum_length - distance) / Decimal(maximum_length)
    return _round_factor(similarity)


def musicbrainz_error_result(
    scan_id: UUID,
    subject_type: str,
    subject_id: UUID,
    reason_code: str,
) -> MusicBrainzMatchResult:
    """Return an error-ready result without adding client orchestration."""
    if subject_type not in _SUBJECT_TYPES:
        raise ValueError("subject_type must be 'album' or 'recording'")
    if reason_code not in _FAILURE_REASONS:
        raise ValueError(
            "error reason_code must be 'request_failed' or 'malformed_response'"
        )
    return _empty_result(
        scan_id,
        subject_type,
        subject_id,
        status="error",
        reason_code=reason_code,
    )


def score_musicbrainz_candidates(
    subjects: MusicBrainzSubjectSet,
    retrieval: MusicBrainzCandidateRetrieval,
) -> MusicBrainzScoringResult:
    """Score and classify complete in-memory subject outcomes for one scan."""
    if subjects.scan_id != retrieval.scan_id:
        raise ValueError("subject and retrieval scan_id values must match")

    album_values = {value.album_group_id: value for value in retrieval.albums}
    recording_values = {value.file_record_id: value for value in retrieval.recordings}
    if len(album_values) != len(retrieval.albums):
        raise ValueError("duplicate album candidate retrieval")
    if len(recording_values) != len(retrieval.recordings):
        raise ValueError("duplicate recording candidate retrieval")
    if {value.album_group_id for value in subjects.albums} != set(album_values):
        raise ValueError("each album subject requires one candidate value")
    if {value.file_record_id for value in subjects.recordings} != set(recording_values):
        raise ValueError("each recording subject requires one candidate value")

    album_scores: list[ScoredAlbumCandidate] = []
    recording_scores: list[ScoredRecordingCandidate] = []
    match_results: list[MusicBrainzMatchResult] = []

    for subject in sorted(subjects.albums, key=lambda value: str(value.album_group_id)):
        subject_id = subject.album_group_id
        scores = _score_album_candidates(
            subjects.scan_id,
            subject,
            album_values[subject_id],
        )
        album_scores.extend(scores)
        match_results.append(
            _classify_candidates(
                subjects.scan_id,
                "album",
                subject_id,
                tuple(
                    (value.candidate.mbid, value.confidence_score) for value in scores
                ),
            )
        )

    for subject in sorted(
        subjects.recordings,
        key=lambda value: str(value.file_record_id),
    ):
        subject_id = subject.file_record_id
        scores = _score_recording_candidates(
            subjects.scan_id,
            subject,
            recording_values[subject_id],
        )
        recording_scores.extend(scores)
        match_results.append(
            _classify_candidates(
                subjects.scan_id,
                "recording",
                subject_id,
                tuple(
                    (value.candidate.mbid, value.confidence_score) for value in scores
                ),
            )
        )

    for subject in sorted(
        subjects.ineligible_recordings,
        key=lambda value: str(value.file_record_id),
    ):
        subject_id = subject.file_record_id
        if subject.reason_code not in {
            "missing_artist",
            "missing_title",
            "unreadable_record",
        }:
            raise ValueError("ineligible recording has an invalid reason_code")
        match_results.append(
            _empty_result(
                subjects.scan_id,
                "recording",
                subject_id,
                status="not_eligible",
                reason_code=subject.reason_code,
            )
        )

    return MusicBrainzScoringResult(
        scan_id=subjects.scan_id,
        album_candidates=tuple(album_scores),
        recording_candidates=tuple(recording_scores),
        match_results=tuple(
            sorted(
                match_results,
                key=lambda value: (value.subject_type, str(value.subject_id)),
            )
        ),
    )


def _score_album_candidates(
    scan_id: UUID,
    subject: AlbumSubject,
    values: AlbumCandidateValues,
) -> tuple[ScoredAlbumCandidate, ...]:
    scored: list[ScoredAlbumCandidate] = []
    for candidate in values.candidates:
        title_similarity = musicbrainz_text_similarity(
            subject.normalized_album,
            candidate.title,
        )
        artist_similarity = musicbrainz_text_similarity(
            subject.normalized_artist,
            candidate.artist_credit,
        )
        year_similarity = _year_similarity(
            subject.release_year,
            candidate.first_release_date,
        )
        confidence_score = _round_score(
            Decimal(45) * title_similarity
            + Decimal(35) * artist_similarity
            + Decimal(15) * year_similarity
            + Decimal(5) * Decimal(candidate.search_score) / Decimal(100)
        )
        scored.append(
            ScoredAlbumCandidate(
                scan_id=scan_id,
                album_group_id=subject.album_group_id,
                candidate_rank=0,
                candidate=candidate,
                title_similarity=title_similarity,
                artist_similarity=artist_similarity,
                year_similarity=year_similarity,
                confidence_score=confidence_score,
            )
        )
    ranked = sorted(
        scored,
        key=lambda value: (
            -value.confidence_score,
            -value.candidate.search_score,
            str(value.candidate.mbid),
        ),
    )
    return tuple(
        replace(value, candidate_rank=rank)
        for rank, value in enumerate(ranked, start=1)
    )


def _score_recording_candidates(
    scan_id: UUID,
    subject: RecordingSubject,
    values: RecordingCandidateValues,
) -> tuple[ScoredRecordingCandidate, ...]:
    scored: list[ScoredRecordingCandidate] = []
    for candidate in values.candidates:
        title_similarity = musicbrainz_text_similarity(
            subject.normalized_title,
            candidate.title,
        )
        artist_similarity = musicbrainz_text_similarity(
            subject.normalized_artist,
            candidate.artist_credit,
        )
        duration_similarity = _duration_similarity(
            subject.duration_seconds,
            candidate.duration_ms,
        )
        (
            album_similarity,
            matched_release_mbid,
            matched_release_title,
        ) = _album_similarity(subject.normalized_album, candidate.releases)
        confidence_score = _round_score(
            Decimal(40) * title_similarity
            + Decimal(30) * artist_similarity
            + Decimal(20) * duration_similarity
            + Decimal(5) * album_similarity
            + Decimal(5) * Decimal(candidate.search_score) / Decimal(100)
        )
        scored.append(
            ScoredRecordingCandidate(
                scan_id=scan_id,
                file_record_id=subject.file_record_id,
                candidate_rank=0,
                candidate=candidate,
                matched_release_mbid=matched_release_mbid,
                matched_release_title=matched_release_title,
                title_similarity=title_similarity,
                artist_similarity=artist_similarity,
                duration_similarity=duration_similarity,
                album_similarity=album_similarity,
                confidence_score=confidence_score,
            )
        )
    ranked = sorted(
        scored,
        key=lambda value: (
            -value.confidence_score,
            -value.candidate.search_score,
            str(value.candidate.mbid),
        ),
    )
    return tuple(
        replace(value, candidate_rank=rank)
        for rank, value in enumerate(ranked, start=1)
    )


def _classify_candidates(
    scan_id: UUID,
    subject_type: str,
    subject_id: UUID,
    candidates: Sequence[tuple[UUID, Decimal]],
) -> MusicBrainzMatchResult:
    if not candidates:
        return _empty_result(
            scan_id,
            subject_type,
            subject_id,
            status="unmatched",
            reason_code="no_candidates",
        )

    top_candidate_mbid, top_score = candidates[0]
    comparison_score = candidates[1][1] if len(candidates) > 1 else _ZERO_SCORE
    margin = _round_score(top_score - comparison_score)
    if (
        top_score >= MUSICBRAINZ_MATCH_THRESHOLD
        and margin >= MUSICBRAINZ_MARGIN_THRESHOLD
    ):
        status = "matched"
        reason_code = "high_confidence_with_margin"
    elif top_score >= MUSICBRAINZ_MATCH_THRESHOLD:
        status = "ambiguous"
        reason_code = "high_confidence_close_candidates"
    elif top_score >= MUSICBRAINZ_AMBIGUOUS_THRESHOLD:
        status = "ambiguous"
        reason_code = "medium_confidence"
    else:
        status = "unmatched"
        reason_code = "below_confidence_threshold"
    return MusicBrainzMatchResult(
        scan_id=scan_id,
        subject_type=subject_type,
        subject_id=subject_id,
        status=status,
        candidate_count=len(candidates),
        top_candidate_mbid=top_candidate_mbid,
        top_confidence_score=top_score,
        confidence_margin=margin,
        reason_code=reason_code,
    )


def _empty_result(
    scan_id: UUID,
    subject_type: str,
    subject_id: UUID,
    *,
    status: str,
    reason_code: str,
) -> MusicBrainzMatchResult:
    return MusicBrainzMatchResult(
        scan_id=scan_id,
        subject_type=subject_type,
        subject_id=subject_id,
        status=status,
        candidate_count=0,
        top_candidate_mbid=None,
        top_confidence_score=None,
        confidence_margin=None,
        reason_code=reason_code,
    )


def _year_similarity(local_year: int | None, first_release_date: str) -> Decimal:
    if local_year is None or len(first_release_date) < 4:
        return _ZERO_FACTOR
    candidate_year_text = first_release_date[:4]
    if not candidate_year_text.isascii() or not candidate_year_text.isdigit():
        return _ZERO_FACTOR
    difference = abs(local_year - int(candidate_year_text))
    return {
        0: Decimal("1.0000"),
        1: Decimal("0.7500"),
        2: Decimal("0.5000"),
        3: Decimal("0.2500"),
    }.get(difference, _ZERO_FACTOR)


def _duration_similarity(
    local_duration_seconds: Decimal | None,
    candidate_duration_ms: int | None,
) -> Decimal:
    if local_duration_seconds is None or candidate_duration_ms is None:
        return _ZERO_FACTOR
    candidate_seconds = Decimal(candidate_duration_ms) / Decimal(1000)
    difference = abs(local_duration_seconds - candidate_seconds)
    similarity = max(Decimal(0), Decimal(1) - difference / Decimal(10))
    return _round_factor(similarity)


def _album_similarity(
    local_album: str,
    releases: Sequence[tuple[UUID, str]],
) -> tuple[Decimal, UUID | None, str]:
    if not normalize_musicbrainz_metadata(local_album) or not releases:
        return _ZERO_FACTOR, None, ""
    choices = tuple(
        (musicbrainz_text_similarity(local_album, title), mbid, title)
        for mbid, title in releases
    )
    similarity, mbid, title = min(
        choices,
        key=lambda value: (-value[0], str(value[1])),
    )
    return similarity, mbid, title


def _levenshtein_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def _round_factor(value: Decimal) -> Decimal:
    return value.quantize(_FACTOR_QUANTUM, rounding=ROUND_HALF_UP)


def _round_score(value: Decimal) -> Decimal:
    return value.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_UP)
