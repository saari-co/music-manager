"""Read-only analysis of records loaded from a library scan CSV.

This module operates only on in-memory ``ScanRecord`` values. It never opens,
stats, modifies, or otherwise accesses the music paths contained in a report.
"""

from __future__ import annotations

import unicodedata
from collections import Counter, defaultdict
from typing import DefaultDict, Dict, List, Sequence, Tuple

from music_manager.models import (
    AnalysisSummary,
    DuplicateGroup,
    LibraryAnalysis,
    MissingMetadataFinding,
    ScanRecord,
)


DEFAULT_DURATION_TOLERANCE = 3.0
DEFAULT_EXTREME_DEPTH = 5
MISSING_METADATA_FIELDS = (
    "artist",
    "title",
    "album",
    "date_year",
    "track_number",
)
QUALITY_BUCKETS = (
    "unknown",
    "under_128",
    "128_to_191",
    "192_to_255",
    "256_to_319",
    "320_plus",
    "lossless_or_uncompressed",
)
LOSSLESS_OR_UNCOMPRESSED_EXTENSIONS = {".flac", ".wav"}


def normalize_metadata(value: str) -> str:
    """Normalize case, Unicode representation, and whitespace for matching."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def find_duplicate_groups(
    records: Sequence[ScanRecord],
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
) -> List[DuplicateGroup]:
    """Group readable tracks with matching identity and nearby durations."""
    if duration_tolerance < 0:
        raise ValueError("duration tolerance must be non-negative")

    identities: DefaultDict[Tuple[str, str], List[ScanRecord]] = defaultdict(list)
    for record in records:
        if record.file_type != "audio" or record.status.casefold() != "ok":
            continue
        artist = normalize_metadata(record.artist)
        title = normalize_metadata(record.title)
        if not artist or not title or record.duration_seconds is None:
            continue
        identities[(artist, title)].append(record)

    candidate_clusters: List[List[ScanRecord]] = []
    for identity in sorted(identities):
        identity_records = sorted(
            identities[identity],
            key=lambda record: (
                record.duration_seconds or 0.0,
                str(record.path).casefold(),
            ),
        )
        cluster: List[ScanRecord] = []
        cluster_start = 0.0

        for record in identity_records:
            duration = record.duration_seconds or 0.0
            if not cluster:
                cluster = [record]
                cluster_start = duration
            elif duration - cluster_start <= duration_tolerance:
                cluster.append(record)
            else:
                if len(cluster) > 1:
                    candidate_clusters.append(cluster)
                cluster = [record]
                cluster_start = duration

        if len(cluster) > 1:
            candidate_clusters.append(cluster)

    return [
        DuplicateGroup(
            group_id=f"DUP-{index:04d}",
            records=tuple(cluster),
        )
        for index, cluster in enumerate(candidate_clusters, start=1)
    ]


def find_missing_metadata(
    records: Sequence[ScanRecord],
) -> List[MissingMetadataFinding]:
    """Return readable audio files missing one or more review fields."""
    findings: List[MissingMetadataFinding] = []
    for record in records:
        if record.file_type != "audio" or record.status.casefold() != "ok":
            continue
        missing_fields = tuple(
            field_name
            for field_name in MISSING_METADATA_FIELDS
            if not getattr(record, field_name).strip()
        )
        if missing_fields:
            findings.append(
                MissingMetadataFinding(
                    record=record,
                    missing_fields=missing_fields,
                )
            )
    return findings


def find_corrupt_files(records: Sequence[ScanRecord]) -> List[ScanRecord]:
    """Return every scan row whose status is not ``ok``."""
    return [record for record in records if record.status.casefold() != "ok"]


def bitrate_bucket(record: ScanRecord) -> str:
    """Classify one audio record into a stable quality bucket."""
    if record.extension in LOSSLESS_OR_UNCOMPRESSED_EXTENSIONS:
        return "lossless_or_uncompressed"
    if record.bitrate_kbps is None:
        return "unknown"
    if record.bitrate_kbps < 128:
        return "under_128"
    if record.bitrate_kbps < 192:
        return "128_to_191"
    if record.bitrate_kbps < 256:
        return "192_to_255"
    if record.bitrate_kbps < 320:
        return "256_to_319"
    return "320_plus"


def summarize_quality(records: Sequence[ScanRecord]) -> Dict[str, int]:
    """Count audio files in each bitrate or format bucket."""
    counts = {bucket: 0 for bucket in QUALITY_BUCKETS}
    for record in records:
        if record.file_type == "audio":
            counts[bitrate_bucket(record)] += 1
    return counts


def summarize_folders(
    records: Sequence[ScanRecord],
    extreme_depth: int = DEFAULT_EXTREME_DEPTH,
) -> Tuple[Dict[int, int], List[ScanRecord], List[ScanRecord]]:
    """Return depth counts, deepest files, and extremely nested files."""
    if extreme_depth < 0:
        raise ValueError("extreme depth must be non-negative")

    audio_records = [record for record in records if record.file_type == "audio"]
    depth_counts = dict(
        sorted(Counter(record.folder_depth for record in audio_records).items())
    )
    deepest_depth = max(depth_counts, default=0)
    deepest_files = sorted(
        (
            record
            for record in audio_records
            if record.folder_depth == deepest_depth
        ),
        key=lambda record: str(record.path).casefold(),
    )
    extreme_files = sorted(
        (
            record
            for record in audio_records
            if record.folder_depth >= extreme_depth
        ),
        key=lambda record: (
            -record.folder_depth,
            str(record.path).casefold(),
        ),
    )
    return depth_counts, deepest_files, extreme_files


def analyze_library(
    records: Sequence[ScanRecord],
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
    extreme_depth: int = DEFAULT_EXTREME_DEPTH,
) -> LibraryAnalysis:
    """Produce all v0.2 findings from scan records."""
    record_list = list(records)
    audio_records = [
        record for record in record_list if record.file_type == "audio"
    ]
    duplicate_groups = find_duplicate_groups(
        record_list, duration_tolerance=duration_tolerance
    )
    missing_metadata = find_missing_metadata(record_list)
    corrupt_files = find_corrupt_files(record_list)
    quality_buckets = summarize_quality(record_list)
    depth_counts, deepest_files, extreme_files = summarize_folders(
        record_list, extreme_depth=extreme_depth
    )
    deepest_depth = max(depth_counts, default=0)

    summary = AnalysisSummary(
        total_audio_files=len(audio_records),
        duplicate_candidate_groups=len(duplicate_groups),
        duplicate_candidate_files=sum(
            len(group.records) for group in duplicate_groups
        ),
        files_with_missing_metadata=len(missing_metadata),
        corrupt_or_unreadable_files=len(corrupt_files),
        low_bitrate_files=quality_buckets["under_128"],
        loose_tracks=sum(record.is_loose_track for record in audio_records),
        deepest_folder_depth=deepest_depth,
        extreme_nesting_files=len(extreme_files),
    )
    return LibraryAnalysis(
        records=record_list,
        duplicate_groups=duplicate_groups,
        missing_metadata=missing_metadata,
        corrupt_files=corrupt_files,
        quality_buckets=quality_buckets,
        folder_depth_counts=depth_counts,
        deepest_files=deepest_files,
        extreme_nesting_files=extreme_files,
        summary=summary,
    )
