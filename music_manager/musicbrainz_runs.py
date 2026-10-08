"""Atomic artifact lifecycle for completed in-memory MusicBrainz matching."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import UUID, uuid4

from music_manager import __version__
from music_manager.artifact_schema import (
    MATCHING_ARTIFACT_NAMES,
    MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
    MUSICBRAINZ_ALBUM_GROUPS_HEADER,
    MUSICBRAINZ_MATCH_RESULTS_HEADER,
    MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
    ArtifactValidationError,
    MusicBrainzAlbumCandidateRow,
    MusicBrainzAlbumGroupRow,
    MusicBrainzMatchResultRow,
    MusicBrainzRecordingCandidateRow,
    ScanManifest,
    required_schema_version,
    validate_artifact_set,
)
from music_manager.matcher import CONSENT_SOURCES, MUSICBRAINZ_CANDIDATE_LIMIT
from music_manager.musicbrainz_client import (
    MUSICBRAINZ_CACHE_MAX_AGE_SECONDS,
    MUSICBRAINZ_CLIENT_POLICY_VERSION,
    MUSICBRAINZ_MAX_ATTEMPTS,
    MUSICBRAINZ_RATE_INTERVAL_SECONDS,
    MUSICBRAINZ_TIMEOUT_SECONDS,
)
from music_manager.musicbrainz_scoring import (
    MUSICBRAINZ_AMBIGUOUS_THRESHOLD,
    MUSICBRAINZ_MARGIN_THRESHOLD,
    MUSICBRAINZ_MATCH_THRESHOLD,
    MUSICBRAINZ_SCORING_MODEL,
    MusicBrainzScoringResult,
)
from music_manager.musicbrainz_subjects import MusicBrainzSubjectSet


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class MusicBrainzArtifactOutcome:
    """One successfully registered MusicBrainz matching artifact family."""

    directory: Path
    manifest: ScanManifest


@dataclass(frozen=True)
class _MatchingReportSpec:
    logical_name: str
    filename: str
    fieldnames: tuple[str, ...]
    rows: tuple[Mapping[str, str], ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("matching artifact clock must return timezone-aware values")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _temporary_path(path: Path) -> Path:
    return path.parent / f".{path.name}.{uuid4().hex}.tmp"


def _validate_row(row_type: type, row: object) -> Mapping[str, str]:
    csv_row = row.to_csv_row()
    row_type.from_csv_row(csv_row)
    return csv_row


def _report_specs(
    subjects: MusicBrainzSubjectSet,
    scoring: MusicBrainzScoringResult,
) -> tuple[_MatchingReportSpec, ...]:
    album_groups = tuple(
        _validate_row(
            MusicBrainzAlbumGroupRow,
            MusicBrainzAlbumGroupRow(
                scan_id=subjects.scan_id,
                album_group_id=subject.album_group_id,
                file_record_id=file_record_id,
            ),
        )
        for subject in sorted(
            subjects.albums,
            key=lambda value: str(value.album_group_id),
        )
        for file_record_id in sorted(subject.member_file_record_ids, key=str)
    )
    album_candidates = tuple(
        _validate_row(
            MusicBrainzAlbumCandidateRow,
            MusicBrainzAlbumCandidateRow(
                scan_id=value.scan_id,
                album_group_id=value.album_group_id,
                candidate_rank=value.candidate_rank,
                release_group_mbid=value.candidate.mbid,
                title=value.candidate.title,
                artist_credit=value.candidate.artist_credit,
                first_release_date=value.candidate.first_release_date,
                primary_type=value.candidate.primary_type,
                secondary_types=value.candidate.secondary_types,
                musicbrainz_search_score=value.candidate.search_score,
                title_similarity=value.title_similarity,
                artist_similarity=value.artist_similarity,
                year_similarity=value.year_similarity,
                confidence_score=value.confidence_score,
            ),
        )
        for value in sorted(
            scoring.album_candidates,
            key=lambda item: (
                str(item.album_group_id),
                item.candidate_rank,
                str(item.candidate.mbid),
            ),
        )
    )
    recording_candidates = tuple(
        _validate_row(
            MusicBrainzRecordingCandidateRow,
            MusicBrainzRecordingCandidateRow(
                scan_id=value.scan_id,
                file_record_id=value.file_record_id,
                candidate_rank=value.candidate_rank,
                recording_mbid=value.candidate.mbid,
                title=value.candidate.title,
                artist_credit=value.candidate.artist_credit,
                duration_ms=value.candidate.duration_ms,
                first_release_date=value.candidate.first_release_date,
                matched_release_mbid=value.matched_release_mbid,
                matched_release_title=value.matched_release_title,
                musicbrainz_search_score=value.candidate.search_score,
                title_similarity=value.title_similarity,
                artist_similarity=value.artist_similarity,
                duration_similarity=value.duration_similarity,
                album_similarity=value.album_similarity,
                confidence_score=value.confidence_score,
            ),
        )
        for value in sorted(
            scoring.recording_candidates,
            key=lambda item: (
                str(item.file_record_id),
                item.candidate_rank,
                str(item.candidate.mbid),
            ),
        )
    )
    match_results = tuple(
        _validate_row(
            MusicBrainzMatchResultRow,
            MusicBrainzMatchResultRow(
                scan_id=value.scan_id,
                subject_type=value.subject_type,
                subject_id=value.subject_id,
                status=value.status,
                candidate_count=value.candidate_count,
                top_candidate_mbid=value.top_candidate_mbid,
                top_confidence_score=value.top_confidence_score,
                confidence_margin=value.confidence_margin,
                reason_code=value.reason_code,
            ),
        )
        for value in sorted(
            scoring.match_results,
            key=lambda item: (item.subject_type, str(item.subject_id)),
        )
    )
    return (
        _MatchingReportSpec(
            logical_name="musicbrainz_album_groups",
            filename="musicbrainz_album_groups.csv",
            fieldnames=MUSICBRAINZ_ALBUM_GROUPS_HEADER,
            rows=album_groups,
        ),
        _MatchingReportSpec(
            logical_name="musicbrainz_album_candidates",
            filename="musicbrainz_album_candidates.csv",
            fieldnames=MUSICBRAINZ_ALBUM_CANDIDATES_HEADER,
            rows=album_candidates,
        ),
        _MatchingReportSpec(
            logical_name="musicbrainz_recording_candidates",
            filename="musicbrainz_recording_candidates.csv",
            fieldnames=MUSICBRAINZ_RECORDING_CANDIDATES_HEADER,
            rows=recording_candidates,
        ),
        _MatchingReportSpec(
            logical_name="musicbrainz_match_results",
            filename="musicbrainz_match_results.csv",
            fieldnames=MUSICBRAINZ_MATCH_RESULTS_HEADER,
            rows=match_results,
        ),
    )


def _validate_inputs(
    manifest: ScanManifest,
    audio_inventory_ids: set[UUID],
    subjects: MusicBrainzSubjectSet,
    scoring: MusicBrainzScoringResult,
) -> None:
    if manifest.scan_id != subjects.scan_id or manifest.scan_id != scoring.scan_id:
        raise ValueError("manifest, subject, and scoring scan_id values must match")

    album_ids = [value.album_group_id for value in subjects.albums]
    recording_ids = [value.file_record_id for value in subjects.recordings]
    ineligible_ids = [value.file_record_id for value in subjects.ineligible_recordings]
    if len(set(album_ids)) != len(album_ids):
        raise ValueError("album subjects must have unique album_group_id values")
    if any(not value.member_file_record_ids for value in subjects.albums):
        raise ValueError("album subjects must contain at least one inventory member")
    if len(set((*recording_ids, *ineligible_ids))) != len(
        (*recording_ids, *ineligible_ids)
    ):
        raise ValueError("recording subjects must have unique file_record_id values")

    member_ids = [
        file_record_id
        for subject in subjects.albums
        for file_record_id in subject.member_file_record_ids
    ]
    if len(set(member_ids)) != len(member_ids):
        raise ValueError("album memberships must not repeat file_record_id values")
    if not set(member_ids).issubset(audio_inventory_ids):
        raise ValueError("album memberships must reference audio inventory rows")
    if not set((*recording_ids, *ineligible_ids)).issubset(audio_inventory_ids):
        raise ValueError("recording subjects must reference audio inventory rows")

    album_candidate_ids = [value.album_group_id for value in scoring.album_candidates]
    recording_candidate_ids = [
        value.file_record_id for value in scoring.recording_candidates
    ]
    if not set(album_candidate_ids).issubset(album_ids):
        raise ValueError("album candidates must reference album subjects")
    if not set(recording_candidate_ids).issubset(recording_ids):
        raise ValueError("recording candidates must reference eligible subjects")
    if any(value.scan_id != manifest.scan_id for value in scoring.album_candidates):
        raise ValueError("album candidate scan_id values must match the manifest")
    if any(value.scan_id != manifest.scan_id for value in scoring.recording_candidates):
        raise ValueError("recording candidate scan_id values must match the manifest")

    expected_result_ids = {
        *(("album", subject_id) for subject_id in album_ids),
        *(("recording", subject_id) for subject_id in recording_ids),
        *(("recording", subject_id) for subject_id in ineligible_ids),
    }
    actual_result_ids = {
        (value.subject_type, value.subject_id) for value in scoring.match_results
    }
    if len(actual_result_ids) != len(scoring.match_results):
        raise ValueError("matching results must have unique subject identities")
    if actual_result_ids != expected_result_ids:
        raise ValueError("matching results must cover every subject exactly once")
    if any(value.scan_id != manifest.scan_id for value in scoring.match_results):
        raise ValueError("matching result scan_id values must match the manifest")

    candidate_counts = Counter(
        ("album", value.album_group_id) for value in scoring.album_candidates
    )
    candidate_counts.update(
        ("recording", value.file_record_id) for value in scoring.recording_candidates
    )
    for result in scoring.match_results:
        if (
            result.candidate_count
            != candidate_counts[(result.subject_type, result.subject_id)]
        ):
            raise ValueError(
                "matching result candidate_count does not match candidates"
            )

    _validate_ranks(album_candidate_ids, scoring.album_candidates)
    _validate_ranks(recording_candidate_ids, scoring.recording_candidates)


def _validate_ranks(subject_ids: Sequence[UUID], values: Sequence[object]) -> None:
    for subject_id in set(subject_ids):
        ranks = sorted(
            value.candidate_rank
            for value in values
            if getattr(value, "album_group_id", None) == subject_id
            or getattr(value, "file_record_id", None) == subject_id
        )
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("candidate ranks must be contiguous and start at one")


def _stage_csv(
    directory: Path,
    spec: _MatchingReportSpec,
) -> tuple[Path, str, int]:
    final_path = directory / spec.filename
    temporary = _temporary_path(final_path)
    digest = hashlib.sha256()
    try:
        with temporary.open("x", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=spec.fieldnames,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(spec.rows)
            output.flush()
            os.fsync(output.fileno())
        with temporary.open("rb") as staged:
            for block in iter(lambda: staged.read(1024 * 1024), b""):
                digest.update(block)
        return temporary, digest.hexdigest(), len(spec.rows)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _matching_configuration(consent_source: str) -> dict[str, object]:
    if consent_source not in CONSENT_SOURCES:
        raise ValueError("consent_source must be 'cli' or 'config'")
    return {
        "client_policy_version": MUSICBRAINZ_CLIENT_POLICY_VERSION,
        "scoring_model_version": MUSICBRAINZ_SCORING_MODEL,
        "candidate_limit": MUSICBRAINZ_CANDIDATE_LIMIT,
        "match_threshold": str(MUSICBRAINZ_MATCH_THRESHOLD),
        "ambiguous_threshold": str(MUSICBRAINZ_AMBIGUOUS_THRESHOLD),
        "margin_threshold": str(MUSICBRAINZ_MARGIN_THRESHOLD),
        "cache_max_age_seconds": int(MUSICBRAINZ_CACHE_MAX_AGE_SECONDS),
        "rate_interval_seconds": MUSICBRAINZ_RATE_INTERVAL_SECONDS,
        "retry_count": MUSICBRAINZ_MAX_ATTEMPTS - 1,
        "timeout_seconds": MUSICBRAINZ_TIMEOUT_SECONDS,
        "consent_source": consent_source,
    }


def _derived_entry(
    spec: _MatchingReportSpec,
    generated_at: str,
    row_count: int,
    digest: str,
    configuration: Mapping[str, object],
) -> dict[str, object]:
    return {
        "filename": spec.filename,
        "role": "derived",
        "application_version": __version__,
        "generated_at": generated_at,
        "row_count": row_count,
        "sha256": digest,
        "configuration": dict(configuration),
    }


def _manifest_without_matching(manifest: ScanManifest) -> ScanManifest:
    data = manifest.to_dict()
    data["artifacts"] = {
        name: entry.to_dict()
        for name, entry in manifest.artifacts.items()
        if name not in MATCHING_ARTIFACT_NAMES
    }
    return ScanManifest.from_dict(data)


def _manifest_with_matching(
    base_manifest: ScanManifest,
    entries: Mapping[str, Mapping[str, object]],
) -> ScanManifest:
    data = base_manifest.to_dict()
    artifacts = dict(data["artifacts"])
    artifacts.update(entries)
    data["artifacts"] = artifacts
    data["schema_version"] = required_schema_version(artifacts)
    return ScanManifest.from_dict(data)


def _atomic_write_manifest(path: Path, manifest: ScanManifest) -> None:
    temporary = _temporary_path(path)
    payload = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(f"{payload}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def register_musicbrainz_artifacts(
    run_directory: Path,
    subjects: MusicBrainzSubjectSet,
    scoring: MusicBrainzScoringResult,
    *,
    consent_source: str,
    clock: Clock = _utc_now,
) -> MusicBrainzArtifactOutcome:
    """Write and atomically register one complete MusicBrainz artifact family."""
    if run_directory.is_symlink():
        raise ArtifactValidationError("scan run directory cannot be a symlink")
    if not run_directory.is_dir():
        raise ArtifactValidationError(
            f"scan run directory does not exist: {run_directory}"
        )

    manifest_path = run_directory / "scan_manifest.json"
    artifacts = validate_artifact_set(manifest_path)
    original_manifest = artifacts.manifest
    if run_directory.name != str(original_manifest.scan_id):
        raise ArtifactValidationError(
            "scan run directory name does not match the manifest scan_id"
        )
    if original_manifest.state not in {"complete", "incomplete"}:
        raise ArtifactValidationError(
            f"scan state {original_manifest.state!r} cannot register matching artifacts"
        )

    configuration = _matching_configuration(consent_source)
    _validate_inputs(
        original_manifest,
        {
            row.file_record_id
            for row in artifacts.library_rows
            if row.file_type == "audio"
        },
        subjects,
        scoring,
    )
    specs = _report_specs(subjects, scoring)
    staged: list[tuple[_MatchingReportSpec, Path | None, str, int]] = []
    backups: dict[Path, Path] = {}
    finalized: list[Path] = []
    base_manifest = _manifest_without_matching(original_manifest)
    manifest_changed = False
    registered = False
    rollback_complete = True
    try:
        for spec in specs:
            temporary, digest, row_count = _stage_csv(run_directory, spec)
            staged.append((spec, temporary, digest, row_count))

        generated_at = _timestamp(clock())
        entries = {
            spec.logical_name: _derived_entry(
                spec,
                generated_at,
                row_count,
                digest,
                configuration,
            )
            for spec, _temporary, digest, row_count in staged
        }
        final_manifest = _manifest_with_matching(base_manifest, entries)

        if base_manifest.artifacts != original_manifest.artifacts:
            _atomic_write_manifest(manifest_path, base_manifest)
            manifest_changed = True

        for spec, _temporary, _digest, _row_count in staged:
            final_path = run_directory / spec.filename
            if final_path.is_symlink() or final_path.exists():
                backup_path = _temporary_path(final_path)
                os.replace(final_path, backup_path)
                backups[final_path] = backup_path

        for index, (spec, temporary, digest, row_count) in enumerate(staged):
            if temporary is None:
                raise AssertionError("matching report was already finalized")
            final_path = run_directory / spec.filename
            os.replace(temporary, final_path)
            finalized.append(final_path)
            staged[index] = (spec, None, digest, row_count)

        _atomic_write_manifest(manifest_path, final_manifest)
        manifest_changed = True
        validated = validate_artifact_set(manifest_path)
        registered = True
        for backup_path in backups.values():
            backup_path.unlink(missing_ok=True)
        return MusicBrainzArtifactOutcome(
            directory=run_directory,
            manifest=validated.manifest,
        )
    except Exception as error:
        rollback_errors: list[Exception] = []
        if manifest_changed:
            try:
                _atomic_write_manifest(manifest_path, base_manifest)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        for final_path in finalized:
            try:
                final_path.unlink(missing_ok=True)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        for final_path, backup_path in backups.items():
            if not backup_path.exists():
                continue
            try:
                os.replace(backup_path, final_path)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if manifest_changed:
            try:
                _atomic_write_manifest(manifest_path, original_manifest)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            rollback_complete = False
            error.add_note(
                "matching artifact rollback also failed: "
                + "; ".join(str(value) for value in rollback_errors)
            )
        raise
    finally:
        for _spec, temporary, _digest, _row_count in staged:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if not registered and rollback_complete:
            for backup_path in backups.values():
                backup_path.unlink(missing_ok=True)
