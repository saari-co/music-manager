"""Typed domain models shared by Music Manager components."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union


CsvValue = Union[str, int, float, bool]


def _csv_text(value: Optional[str]) -> str:
    """Normalize a possibly empty CSV cell."""
    return value.strip() if value is not None else ""


def _optional_int(value: Optional[str]) -> Optional[int]:
    """Parse an optional integer from a CSV cell."""
    if value is None or not value.strip():
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _optional_float(value: Optional[str]) -> Optional[float]:
    """Parse an optional float from a CSV cell."""
    if value is None or not value.strip():
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _csv_bool(value: Optional[str]) -> bool:
    """Parse a boolean emitted by ``csv.DictWriter``."""
    if value is None:
        return False
    return value.strip().casefold() in {"1", "true", "yes"}


@dataclass
class ScanRecord:
    """One audio file or archive discovered during a library scan."""

    path: Path
    extension: str
    file_type: str
    file_size_bytes: Optional[int] = None
    folder_depth: int = 0
    artist: str = ""
    title: str = ""
    album: str = ""
    date_year: str = ""
    track_number: str = ""
    bitrate_kbps: Optional[float] = None
    duration_seconds: Optional[float] = None
    is_loose_track: bool = False
    is_archive: bool = False
    status: str = "ok"
    error: str = ""

    @classmethod
    def from_csv_row(
        cls, row: Mapping[str, Optional[str]]
    ) -> "ScanRecord":
        """Build a scan record without accessing the referenced file path."""
        return cls(
            path=Path(_csv_text(row.get("path"))),
            extension=_csv_text(row.get("extension")).lower(),
            file_type=_csv_text(row.get("file_type")).lower(),
            file_size_bytes=_optional_int(row.get("file_size_bytes")),
            folder_depth=_optional_int(row.get("folder_depth")) or 0,
            artist=_csv_text(row.get("artist")),
            title=_csv_text(row.get("title")),
            album=_csv_text(row.get("album")),
            date_year=_csv_text(row.get("date_year")),
            track_number=_csv_text(row.get("track_number")),
            bitrate_kbps=_optional_float(row.get("bitrate_kbps")),
            duration_seconds=_optional_float(row.get("duration_seconds")),
            is_loose_track=_csv_bool(row.get("is_loose_track")),
            is_archive=_csv_bool(row.get("is_archive")),
            status=_csv_text(row.get("status")) or "ok",
            error=_csv_text(row.get("error")),
        )

    def to_csv_row(self) -> Dict[str, CsvValue]:
        """Return a stable, serialization-ready representation."""
        return {
            "path": str(self.path),
            "extension": self.extension,
            "file_type": self.file_type,
            "file_size_bytes": self.file_size_bytes
            if self.file_size_bytes is not None
            else "",
            "folder_depth": self.folder_depth,
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "date_year": self.date_year,
            "track_number": self.track_number,
            "bitrate_kbps": self.bitrate_kbps
            if self.bitrate_kbps is not None
            else "",
            "duration_seconds": self.duration_seconds
            if self.duration_seconds is not None
            else "",
            "is_loose_track": self.is_loose_track,
            "is_archive": self.is_archive,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class ScanSummary:
    """Aggregate counts shown after a scan."""

    audio_count: int
    archive_count: int
    loose_track_count: int
    file_error_count: int
    directory_error_count: int

    @classmethod
    def from_records(
        cls, records: Sequence[ScanRecord], directory_error_count: int
    ) -> "ScanSummary":
        """Calculate summary counts from scan records."""
        return cls(
            audio_count=sum(record.file_type == "audio" for record in records),
            archive_count=sum(record.file_type == "archive" for record in records),
            loose_track_count=sum(record.is_loose_track for record in records),
            file_error_count=sum(record.status == "error" for record in records),
            directory_error_count=directory_error_count,
        )


@dataclass
class ScanResult:
    """Complete result of one read-only library scan."""

    source: Path
    records: List[ScanRecord] = field(default_factory=list)
    directory_errors: List[str] = field(default_factory=list)

    @property
    def summary(self) -> ScanSummary:
        """Return aggregate counts for this result."""
        return ScanSummary.from_records(
            self.records, directory_error_count=len(self.directory_errors)
        )


@dataclass(frozen=True)
class DuplicateGroup:
    """Tracks that share normalized identity and similar duration."""

    group_id: str
    records: Tuple[ScanRecord, ...]


@dataclass(frozen=True)
class MissingMetadataFinding:
    """A readable audio record and the metadata fields it lacks."""

    record: ScanRecord
    missing_fields: Tuple[str, ...]


@dataclass(frozen=True)
class AnalysisSummary:
    """Aggregate counts printed after library analysis."""

    total_audio_files: int
    duplicate_candidate_groups: int
    duplicate_candidate_files: int
    files_with_missing_metadata: int
    corrupt_or_unreadable_files: int
    low_bitrate_files: int
    loose_tracks: int
    deepest_folder_depth: int
    extreme_nesting_files: int


@dataclass
class LibraryAnalysis:
    """Read-only findings produced from an existing scan report."""

    records: List[ScanRecord]
    duplicate_groups: List[DuplicateGroup]
    missing_metadata: List[MissingMetadataFinding]
    corrupt_files: List[ScanRecord]
    quality_buckets: Dict[str, int]
    folder_depth_counts: Dict[int, int]
    deepest_files: List[ScanRecord]
    extreme_nesting_files: List[ScanRecord]
    summary: AnalysisSummary
