"""Offline tests for deterministic MusicBrainz scoring and classification."""

from __future__ import annotations

import unittest
from decimal import Decimal
from uuid import UUID, uuid5

from music_manager.matcher import RecordingSearchResult, ReleaseGroupSearchResult
from music_manager.musicbrainz_scoring import (
    MUSICBRAINZ_SCORING_MODEL,
    musicbrainz_error_result,
    musicbrainz_text_similarity,
    score_musicbrainz_candidates,
)
from music_manager.musicbrainz_subjects import (
    AlbumCandidateValues,
    AlbumSubject,
    IneligibleRecordingSubject,
    MusicBrainzCandidateRetrieval,
    MusicBrainzSubjectSet,
    RecordingCandidateValues,
    RecordingSubject,
)


SCAN_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ALBUM_ID = uuid5(SCAN_ID, "album-one")
SECOND_ALBUM_ID = uuid5(SCAN_ID, "album-two")
RECORDING_ID = uuid5(SCAN_ID, "recording-one")
SECOND_RECORDING_ID = uuid5(SCAN_ID, "recording-two")


def _album_subject(
    album_group_id: UUID = ALBUM_ID,
    *,
    artist: str = "glass harbor",
    album: str = "night signal",
    release_year: int | None = 2031,
) -> AlbumSubject:
    return AlbumSubject(
        album_group_id=album_group_id,
        query_artist=artist.title(),
        query_album=album.title(),
        normalized_artist=artist,
        normalized_album=album,
        release_year=release_year,
        member_file_record_ids=(uuid5(album_group_id, "member"),),
    )


def _recording_subject(
    file_record_id: UUID = RECORDING_ID,
    *,
    artist: str = "glass harbor",
    title: str = "silver signal",
    album: str = "night signal",
    duration: Decimal | None = Decimal("200"),
) -> RecordingSubject:
    return RecordingSubject(
        file_record_id=file_record_id,
        query_artist=artist.title(),
        query_title=title.title(),
        normalized_artist=artist,
        normalized_title=title,
        normalized_album=album,
        duration_seconds=duration,
        release_year=2031,
    )


def _release_group(
    mbid: str,
    *,
    title: str = "Night Signal",
    artist: str = "Glass Harbor",
    date: str = "2031",
    search_score: int = 100,
) -> ReleaseGroupSearchResult:
    return ReleaseGroupSearchResult(
        mbid=UUID(mbid),
        title=title,
        artist_credit=artist,
        first_release_date=date,
        search_score=search_score,
    )


def _recording(
    mbid: str,
    *,
    title: str = "Silver Signal",
    artist: str = "Glass Harbor",
    duration_ms: int | None = 200_000,
    releases: tuple[tuple[UUID, str], ...] = (),
    search_score: int = 100,
) -> RecordingSearchResult:
    return RecordingSearchResult(
        mbid=UUID(mbid),
        title=title,
        artist_credit=artist,
        duration_ms=duration_ms,
        releases=releases,
        search_score=search_score,
    )


def _subjects(
    *,
    albums: tuple[AlbumSubject, ...] = (),
    recordings: tuple[RecordingSubject, ...] = (),
    ineligible: tuple[IneligibleRecordingSubject, ...] = (),
) -> MusicBrainzSubjectSet:
    return MusicBrainzSubjectSet(
        scan_id=SCAN_ID,
        albums=albums,
        recordings=recordings,
        ineligible_recordings=ineligible,
    )


def _retrieval(
    *,
    albums: tuple[AlbumCandidateValues, ...] = (),
    recordings: tuple[RecordingCandidateValues, ...] = (),
    scan_id: UUID = SCAN_ID,
) -> MusicBrainzCandidateRetrieval:
    return MusicBrainzCandidateRetrieval(
        scan_id=scan_id,
        albums=albums,
        recordings=recordings,
    )


class MusicBrainzEvidenceTests(unittest.TestCase):
    def test_text_similarity_normalizes_unicode_and_rounds_half_up(self) -> None:
        self.assertEqual(MUSICBRAINZ_SCORING_MODEL, "musicbrainz-confidence-v1")
        self.assertEqual(
            musicbrainz_text_similarity("Straße  Ｓignal", "STRASSE signal"),
            Decimal("1.0000"),
        )
        self.assertEqual(
            musicbrainz_text_similarity("abcdefghi", "abcdefghx"),
            Decimal("0.8889"),
        )
        self.assertEqual(
            musicbrainz_text_similarity("", ""),
            Decimal("0.0000"),
        )

    def test_album_score_uses_rounded_text_year_and_search_evidence(self) -> None:
        subject = _album_subject(album="abcdefghi")
        candidate = _release_group(
            "11111111-1111-4111-8111-111111111111",
            title="abcdefghx",
            date="2032-07",
            search_score=81,
        )

        scored = score_musicbrainz_candidates(
            _subjects(albums=(subject,)),
            _retrieval(
                albums=(
                    AlbumCandidateValues(
                        album_group_id=ALBUM_ID,
                        candidates=(candidate,),
                    ),
                )
            ),
        )

        value = scored.album_candidates[0]
        self.assertEqual(value.candidate_rank, 1)
        self.assertEqual(value.title_similarity, Decimal("0.8889"))
        self.assertEqual(value.artist_similarity, Decimal("1.0000"))
        self.assertEqual(value.year_similarity, Decimal("0.7500"))
        self.assertEqual(value.confidence_score, Decimal("90.30"))
        self.assertEqual(scored.match_results[0].status, "matched")
        self.assertEqual(
            scored.match_results[0].reason_code,
            "high_confidence_with_margin",
        )
        self.assertEqual(
            scored.match_results[0].confidence_margin,
            Decimal("90.30"),
        )

    def test_recording_score_selects_lexical_release_on_equal_album_evidence(
        self,
    ) -> None:
        lexical_release = UUID("11111111-1111-4111-8111-111111111111")
        later_release = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
        candidate = _recording(
            "22222222-2222-4222-8222-222222222222",
            duration_ms=203_333,
            releases=(
                (later_release, "Night Signal"),
                (lexical_release, "night signal"),
            ),
            search_score=81,
        )

        scored = score_musicbrainz_candidates(
            _subjects(recordings=(_recording_subject(),)),
            _retrieval(
                recordings=(
                    RecordingCandidateValues(
                        file_record_id=RECORDING_ID,
                        candidates=(candidate,),
                    ),
                )
            ),
        )

        value = scored.recording_candidates[0]
        self.assertEqual(value.title_similarity, Decimal("1.0000"))
        self.assertEqual(value.artist_similarity, Decimal("1.0000"))
        self.assertEqual(value.duration_similarity, Decimal("0.6667"))
        self.assertEqual(value.album_similarity, Decimal("1.0000"))
        self.assertEqual(value.matched_release_mbid, lexical_release)
        self.assertEqual(value.matched_release_title, "night signal")
        self.assertEqual(value.confidence_score, Decimal("92.38"))

    def test_missing_recording_evidence_scores_zero_without_ineligibility(
        self,
    ) -> None:
        subject = _recording_subject(album="", duration=None)
        candidate = _recording(
            "33333333-3333-4333-8333-333333333333",
            duration_ms=200_000,
            releases=(
                (
                    UUID("44444444-4444-4444-8444-444444444444"),
                    "Night Signal",
                ),
            ),
            search_score=0,
        )

        scored = score_musicbrainz_candidates(
            _subjects(recordings=(subject,)),
            _retrieval(
                recordings=(
                    RecordingCandidateValues(
                        file_record_id=RECORDING_ID,
                        candidates=(candidate,),
                    ),
                )
            ),
        )

        value = scored.recording_candidates[0]
        self.assertEqual(value.duration_similarity, Decimal("0.0000"))
        self.assertEqual(value.album_similarity, Decimal("0.0000"))
        self.assertIsNone(value.matched_release_mbid)
        self.assertEqual(value.matched_release_title, "")
        self.assertEqual(value.confidence_score, Decimal("70.00"))
        self.assertEqual(scored.match_results[0].status, "ambiguous")
        self.assertEqual(scored.match_results[0].reason_code, "medium_confidence")


class MusicBrainzClassificationTests(unittest.TestCase):
    def test_candidate_rank_and_close_classification_ignore_response_order(
        self,
    ) -> None:
        subject = _album_subject(album="abcdefghi")
        high_search = _release_group(
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
            title="abcdefghx",
            search_score=100,
        )
        lexical_tie = _release_group(
            "00000000-0000-4000-8000-000000000000",
            title="abcdefghi",
            search_score=0,
        )
        later_tie = _release_group(
            "11111111-1111-4111-8111-111111111111",
            title="abcdefghi",
            search_score=0,
        )

        def run(
            candidates: tuple[ReleaseGroupSearchResult, ...],
        ) -> object:
            return score_musicbrainz_candidates(
                _subjects(albums=(subject,)),
                _retrieval(
                    albums=(
                        AlbumCandidateValues(
                            album_group_id=ALBUM_ID,
                            candidates=candidates,
                        ),
                    )
                ),
            )

        forward = run((high_search, lexical_tie, later_tie))
        reversed_result = run((later_tie, lexical_tie, high_search))

        self.assertEqual(forward, reversed_result)
        self.assertEqual(
            tuple(value.candidate.mbid for value in forward.album_candidates),
            (high_search.mbid, lexical_tie.mbid, later_tie.mbid),
        )
        self.assertTrue(
            all(
                value.confidence_score == Decimal("95.00")
                for value in forward.album_candidates
            )
        )
        result = forward.match_results[0]
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.reason_code, "high_confidence_close_candidates")
        self.assertEqual(result.confidence_margin, Decimal("0.00"))

    def test_low_and_empty_candidate_sets_are_unmatched(self) -> None:
        low_subject = _album_subject()
        empty_subject = _album_subject(SECOND_ALBUM_ID, album="silent map")
        low_candidate = _release_group(
            "55555555-5555-4555-8555-555555555555",
            title="Unrelated",
            artist="",
            date="",
            search_score=100,
        )

        scored = score_musicbrainz_candidates(
            _subjects(albums=(empty_subject, low_subject)),
            _retrieval(
                albums=(
                    AlbumCandidateValues(
                        album_group_id=SECOND_ALBUM_ID,
                        candidates=(),
                    ),
                    AlbumCandidateValues(
                        album_group_id=ALBUM_ID,
                        candidates=(low_candidate,),
                    ),
                )
            ),
        )

        results = {value.subject_id: value for value in scored.match_results}
        low = results[ALBUM_ID]
        self.assertEqual(low.status, "unmatched")
        self.assertEqual(low.reason_code, "below_confidence_threshold")
        self.assertEqual(low.candidate_count, 1)
        self.assertIsNotNone(low.top_confidence_score)
        empty = results[SECOND_ALBUM_ID]
        self.assertEqual(empty.status, "unmatched")
        self.assertEqual(empty.reason_code, "no_candidates")
        self.assertEqual(empty.candidate_count, 0)
        self.assertIsNone(empty.top_candidate_mbid)
        self.assertIsNone(empty.top_confidence_score)
        self.assertIsNone(empty.confidence_margin)

    def test_ineligible_and_error_ready_results_use_stable_shapes(self) -> None:
        unreadable_id = uuid5(SCAN_ID, "unreadable")
        missing_artist_id = uuid5(SCAN_ID, "missing-artist")
        missing_title_id = uuid5(SCAN_ID, "missing-title")
        subjects = _subjects(
            ineligible=(
                IneligibleRecordingSubject(unreadable_id, "unreadable_record"),
                IneligibleRecordingSubject(missing_artist_id, "missing_artist"),
                IneligibleRecordingSubject(missing_title_id, "missing_title"),
            ),
        )
        errors = (
            musicbrainz_error_result(
                SCAN_ID,
                "album",
                ALBUM_ID,
                "malformed_response",
            ),
            musicbrainz_error_result(
                SCAN_ID,
                "recording",
                RECORDING_ID,
                "request_failed",
            ),
        )
        scored = score_musicbrainz_candidates(subjects, _retrieval())
        results = {
            (value.subject_type, value.subject_id): value
            for value in (*errors, *scored.match_results)
        }
        self.assertEqual(
            results[("album", ALBUM_ID)].reason_code,
            "malformed_response",
        )
        self.assertEqual(
            results[("recording", RECORDING_ID)].reason_code,
            "request_failed",
        )
        for result in errors:
            self.assertEqual(result.status, "error")
            self.assertEqual(result.candidate_count, 0)
            self.assertIsNone(result.top_candidate_mbid)
        for subject in subjects.ineligible_recordings:
            result = results[("recording", subject.file_record_id)]
            self.assertEqual(result.status, "not_eligible")
            self.assertEqual(result.reason_code, subject.reason_code)
            self.assertEqual(result.candidate_count, 0)


if __name__ == "__main__":
    unittest.main()
