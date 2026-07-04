"""Typed domain models shared by Music Manager components."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union
from uuid import UUID, uuid4

from music_manager.artifact_schema import LibraryScanRow, ScanErrorRow


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
    modified_time_ns: Optional[int] = None
    scan_id: Optional[UUID] = None
    file_record_id: Optional[UUID] = None
    file_fingerprint: str = ""
    relative_path: str = ""
    artist: str = ""
    album_artist: str = ""
    title: str = ""
    album: str = ""
    date: str = ""
    date_year: str = ""
    track_number: str = ""
    release_year: Optional[int] = None
    parsed_track_number: Optional[int] = None
    track_total: Optional[int] = None
    disc_number: Optional[int] = None
    disc_total: Optional[int] = None
    genre: str = ""
    composer: str = ""
    is_compilation: Optional[bool] = None
    codec: str = ""
    container: str = ""
    bitrate_kbps: Optional[float] = None
    duration_seconds: Optional[float] = None
    sample_rate_hz: Optional[int] = None
    bit_depth: Optional[int] = None
    channels: Optional[int] = None
    is_archive: bool = False
    status: str = "ok"
    error: str = ""

    @classmethod
    def from_csv_row(cls, row: Mapping[str, Optional[str]]) -> "ScanRecord":
        """Build a scan record without accessing the referenced file path."""
        return cls(
            path=Path(_csv_text(row.get("path"))),
            extension=_csv_text(row.get("extension")).lower(),
            file_type=_csv_text(row.get("file_type")).lower(),
            file_size_bytes=_optional_int(row.get("file_size_bytes")),
            artist=_csv_text(row.get("artist")),
            title=_csv_text(row.get("title")),
            album=_csv_text(row.get("album")),
            date_year=_csv_text(row.get("date_year")),
            track_number=_csv_text(row.get("track_number")),
            bitrate_kbps=_optional_float(row.get("bitrate_kbps")),
            duration_seconds=_optional_float(row.get("duration_seconds")),
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
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "date_year": self.date_year,
            "track_number": self.track_number,
            "bitrate_kbps": self.bitrate_kbps if self.bitrate_kbps is not None else "",
            "duration_seconds": self.duration_seconds
            if self.duration_seconds is not None
            else "",
            "is_archive": self.is_archive,
            "status": self.status,
            "error": self.error,
        }

    def to_library_scan_row(self) -> LibraryScanRow:
        """Return this discovered record as a validated schema 1 row."""
        if self.scan_id is None:
            raise ValueError("scan record does not have a scan_id")
        if self.file_record_id is None:
            raise ValueError("scan record does not have a file_record_id")
        if not self.relative_path:
            raise ValueError("scan record does not have a relative path")
        return LibraryScanRow.from_csv_row(
            {
                "scan_id": str(self.scan_id),
                "file_record_id": str(self.file_record_id),
                "file_fingerprint": self.file_fingerprint,
                "path": self.relative_path,
                "extension": self.extension,
                "file_type": self.file_type,
                "file_size_bytes": _optional_csv(self.file_size_bytes),
                "modified_time_ns": _optional_csv(self.modified_time_ns),
                "artist": self.artist,
                "album_artist": self.album_artist,
                "title": self.title,
                "album": self.album,
                "date": self.date,
                "release_year": _optional_csv(self.release_year),
                "track_number": _optional_csv(self.parsed_track_number),
                "track_total": _optional_csv(self.track_total),
                "disc_number": _optional_csv(self.disc_number),
                "disc_total": _optional_csv(self.disc_total),
                "genre": self.genre,
                "composer": self.composer,
                "is_compilation": (
                    ""
                    if self.is_compilation is None
                    else str(self.is_compilation).lower()
                ),
                "codec": self.codec,
                "container": self.container,
                "bitrate_kbps": _optional_decimal_csv(self.bitrate_kbps),
                "duration_seconds": _optional_decimal_csv(self.duration_seconds),
                "sample_rate_hz": _optional_csv(self.sample_rate_hz),
                "bit_depth": _optional_csv(self.bit_depth),
                "channels": _optional_csv(self.channels),
                "record_status": self.status,
            },
            location=f"scan record {self.relative_path!r}",
        )


def _optional_csv(value: Optional[int]) -> str:
    return "" if value is None else str(value)


def _optional_decimal_csv(value: Optional[float]) -> str:
    return "" if value is None else str(Decimal(str(value)))


@dataclass(frozen=True)
class ScanFinding:
    """One structured discovery or extraction finding."""

    path: str
    stage: str
    severity: str
    error_code: str
    message: str
    file_record_id: Optional[UUID] = None

    def to_scan_error_row(self, scan_id: UUID) -> ScanErrorRow:
        """Return this finding as a validated schema 1 row."""
        return ScanErrorRow.from_csv_row(
            {
                "scan_id": str(scan_id),
                "file_record_id": (
                    "" if self.file_record_id is None else str(self.file_record_id)
                ),
                "path": self.path,
                "stage": self.stage,
                "severity": self.severity,
                "error_code": self.error_code,
                "message": self.message,
            },
            location=f"scan finding {self.error_code!r}",
        )


@dataclass(frozen=True)
class ScanSummary:
    """Aggregate counts shown after a scan."""

    root_library_total: int
    archive_count: int
    file_error_count: int
    directory_error_count: int

    @property
    def audio_count(self) -> int:
        """Compatibility alias for the Root Library track total."""
        return self.root_library_total

    @classmethod
    def from_records(
        cls, records: Sequence[ScanRecord], directory_error_count: int
    ) -> "ScanSummary":
        """Calculate summary counts from scan records."""
        return cls(
            root_library_total=sum(record.file_type == "audio" for record in records),
            archive_count=sum(record.file_type == "archive" for record in records),
            file_error_count=sum(record.status == "error" for record in records),
            directory_error_count=directory_error_count,
        )


@dataclass
class ScanResult:
    """Complete result of one read-only library scan."""

    source: Path
    records: List[ScanRecord] = field(default_factory=list)
    directory_errors: List[str] = field(default_factory=list)
    scan_id: UUID = field(default_factory=uuid4)
    findings: List[ScanFinding] = field(default_factory=list)

    @property
    def summary(self) -> ScanSummary:
        """Return aggregate counts for this result."""
        return ScanSummary.from_records(
            self.records, directory_error_count=len(self.directory_errors)
        )

    def to_library_scan_rows(self) -> Tuple[LibraryScanRow, ...]:
        """Return validated schema 1 inventory rows without writing files."""
        return tuple(record.to_library_scan_row() for record in self.records)

    def to_scan_error_rows(self) -> Tuple[ScanErrorRow, ...]:
        """Return validated schema 1 finding rows without writing files."""
        return tuple(
            finding.to_scan_error_row(self.scan_id) for finding in self.findings
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

    root_library_total: int
    duplicate_candidate_groups: int
    duplicate_candidate_files: int
    files_with_missing_metadata: int
    corrupt_or_unreadable_files: int
    low_bitrate_files: int

    @property
    def total_audio_files(self) -> int:
        """Compatibility alias for the Root Library track total."""
        return self.root_library_total


@dataclass
class LibraryAnalysis:
    """Read-only findings produced from an existing scan report."""

    records: List[ScanRecord]
    duplicate_groups: List[DuplicateGroup]
    missing_metadata: List[MissingMetadataFinding]
    corrupt_files: List[ScanRecord]
    quality_buckets: Dict[str, int]
    metadata_completeness: Dict[str, float]
    summary: AnalysisSummary
