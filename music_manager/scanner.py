"""Read-only music library discovery and metadata extraction."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from music_manager.models import ScanRecord, ScanResult
from music_manager.utils import clean_error, first_tag

try:
    import mutagen
except ImportError:  # The CLI reports the missing optional runtime dependency.
    mutagen = None


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".wav"}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | {".zip"}
MetadataLoader = Callable[[Path], Any]


def metadata_reader_available() -> bool:
    """Return whether Mutagen is available to read audio metadata."""
    return mutagen is not None


def _load_metadata(path: Path) -> Any:
    """Load one file with Mutagen without saving or modifying it."""
    if mutagen is None:
        raise RuntimeError("Mutagen is not installed")
    return mutagen.File(path)


def _is_ignored(
    path: Path, source: Path, ignore_patterns: Sequence[str]
) -> bool:
    """Match a path against configured source-relative ignore patterns."""
    relative_path = path.relative_to(source).as_posix()
    for raw_pattern in ignore_patterns:
        pattern = raw_pattern.replace("\\", "/").rstrip("/")
        if pattern.startswith("./"):
            pattern = pattern[2:]
        if not pattern:
            continue
        if relative_path == pattern or relative_path.startswith(f"{pattern}/"):
            return True
        if fnmatch.fnmatchcase(relative_path, pattern):
            return True
        if "/" not in pattern and fnmatch.fnmatchcase(path.name, pattern):
            return True
    return False


def discover_files(
    source: Path, ignore_patterns: Sequence[str] = ()
) -> Tuple[List[Path], List[str]]:
    """Find supported files recursively and collect inaccessible-directory errors."""
    paths: List[Path] = []
    directory_errors: List[str] = []

    def record_walk_error(error: OSError) -> None:
        directory_errors.append(clean_error(error))

    for root, directories, filenames in os.walk(
        source,
        topdown=True,
        onerror=record_walk_error,
        followlinks=False,
    ):
        directories.sort(key=str.casefold)
        directories[:] = [
            directory
            for directory in directories
            if not _is_ignored(
                Path(root) / directory,
                source,
                ignore_patterns,
            )
        ]
        for filename in sorted(filenames, key=str.casefold):
            path = Path(root) / filename
            if _is_ignored(path, source, ignore_patterns):
                continue
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.append(path)

    return paths, directory_errors


def scan_audio_file(
    path: Path,
    metadata_loader: Optional[MetadataLoader] = None,
) -> ScanRecord:
    """Read one audio file and return an error record if reading fails."""
    record = ScanRecord(
        path=path,
        extension=path.suffix.lower(),
        file_type="audio",
    )

    try:
        record.file_size_bytes = path.stat().st_size
        loader = metadata_loader or _load_metadata
        audio = loader(path)
        if audio is None:
            raise ValueError("Mutagen could not identify the audio format")

        tags = getattr(audio, "tags", None)
        record.artist = first_tag(
            tags,
            ("artist", "albumartist", "TPE1", "TPE2", "\xa9ART", "aART"),
        )
        record.title = first_tag(tags, ("title", "TIT2", "\xa9nam"))
        record.album = first_tag(tags, ("album", "TALB", "\xa9alb"))
        record.date_year = first_tag(
            tags,
            (
                "date",
                "year",
                "originaldate",
                "originalyear",
                "TDRC",
                "TYER",
                "\xa9day",
            ),
        )
        record.track_number = first_tag(
            tags, ("tracknumber", "TRCK", "trkn")
        )

        info = getattr(audio, "info", None)
        bitrate = getattr(info, "bitrate", None)
        duration = getattr(info, "length", None)
        if isinstance(bitrate, (int, float)):
            record.bitrate_kbps = round(bitrate / 1000, 2)
        if isinstance(duration, (int, float)):
            record.duration_seconds = round(duration, 3)
    except Exception as error:
        record.status = "error"
        record.error = clean_error(error)

    return record


def scan_archive(path: Path) -> ScanRecord:
    """Record a ZIP archive without opening or extracting it."""
    record = ScanRecord(
        path=path,
        extension=path.suffix.lower(),
        file_type="archive",
        is_archive=True,
    )
    try:
        record.file_size_bytes = path.stat().st_size
    except Exception as error:
        record.status = "error"
        record.error = clean_error(error)
    return record


def scan_library(
    source: Path,
    metadata_loader: Optional[MetadataLoader] = None,
    ignore_patterns: Sequence[str] = (),
) -> ScanResult:
    """Scan a source directory without modifying any discovered file."""
    paths, directory_errors = discover_files(
        source, ignore_patterns=ignore_patterns
    )
    records: List[ScanRecord] = []

    for path in paths:
        if path.suffix.lower() == ".zip":
            records.append(scan_archive(path))
        else:
            records.append(
                scan_audio_file(
                    path,
                    metadata_loader=metadata_loader,
                )
            )

    return ScanResult(
        source=source,
        records=records,
        directory_errors=directory_errors,
    )
