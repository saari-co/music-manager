"""Typed domain models shared by Music Manager components."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union


CsvValue = Union[str, int, float, bool]


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
