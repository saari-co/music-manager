"""Library-source naming and conservative top-level aggregation."""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path
from typing import Dict, Sequence

from music_manager.models import ScanRecord


APPLE_MUSIC_PREFIX = ("music", "media.localized", "music")
ROOT_LIBRARY_SOURCE = "Root Library"
SOURCE_AGGREGATION_MIN_LIBRARY_SIZE = 1000
SOURCE_AGGREGATION_MAX_THRESHOLD = 200
SOURCE_AGGREGATION_MIN_THRESHOLD = 20


def source_name_for_relative_path(
    path: Path, fallback: str = "Source"
) -> str:
    """Infer a source label from a path relative to the scanned root."""
    parts = path.parts
    normalized_parts = tuple(part.casefold() for part in parts[:3])
    if normalized_parts == APPLE_MUSIC_PREFIX:
        return "Apple Music"
    if len(parts) > 1:
        return parts[0]
    return fallback


def consolidate_library_sources(
    records: Sequence[ScanRecord],
) -> Dict[str, int]:
    """Roll small top-level groups into one root source for large libraries."""
    audio_records = [
        record for record in records if record.file_type == "audio"
    ]
    if not audio_records:
        return {}

    raw_counts = Counter(
        record.library_source or ROOT_LIBRARY_SOURCE
        for record in audio_records
    )
    if len(audio_records) >= SOURCE_AGGREGATION_MIN_LIBRARY_SIZE:
        threshold = min(
            SOURCE_AGGREGATION_MAX_THRESHOLD,
            max(
                SOURCE_AGGREGATION_MIN_THRESHOLD,
                math.ceil(len(audio_records) * 0.01),
            ),
        )
        retained_sources = {
            source
            for source, count in raw_counts.items()
            if count >= threshold
        }
        for record in records:
            source = record.library_source or ROOT_LIBRARY_SOURCE
            if source not in retained_sources:
                record.library_source = ROOT_LIBRARY_SOURCE

    return dict(
        sorted(
            Counter(
                record.library_source or ROOT_LIBRARY_SOURCE
                for record in audio_records
            ).items(),
            key=lambda item: item[0].casefold(),
        )
    )
