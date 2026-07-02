"""Read-only music library discovery and metadata extraction."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from music_manager.models import ScanRecord, ScanResult
from music_manager.utils import clean_error, first_tag, folder_depth

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


def discover_files(source: Path) -> Tuple[List[Path], List[str]]:
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
        for filename in sorted(filenames, key=str.casefold):
            path = Path(root) / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.append(path)

    return paths, directory_errors


def scan_audio_file(
    path: Path,
    source: Path,
    is_loose_track: bool,
    metadata_loader: Optional[MetadataLoader] = None,
) -> ScanRecord:
    """Read one audio file and return an error record if reading fails."""
    record = ScanRecord(
        path=path,
        extension=path.suffix.lower(),
        file_type="audio",
        folder_depth=folder_depth(path, source),
        is_loose_track=is_loose_track,
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


def scan_archive(path: Path, source: Path) -> ScanRecord:
    """Record a ZIP archive without opening or extracting it."""
    record = ScanRecord(
        path=path,
        extension=path.suffix.lower(),
        file_type="archive",
        folder_depth=folder_depth(path, source),
        is_archive=True,
    )
    try:
        record.file_size_bytes = path.stat().st_size
    except Exception as error:
        record.status = "error"
        record.error = clean_error(error)
    return record


def scan_library(
    source: Path, metadata_loader: Optional[MetadataLoader] = None
) -> ScanResult:
    """Scan a source directory without modifying any discovered file."""
    paths, directory_errors = discover_files(source)
    audio_files_per_folder = Counter(
        path.parent for path in paths if path.suffix.lower() in AUDIO_EXTENSIONS
    )
    records: List[ScanRecord] = []

    for path in paths:
        if path.suffix.lower() == ".zip":
            records.append(scan_archive(path, source))
        else:
            records.append(
                scan_audio_file(
                    path,
                    source,
                    is_loose_track=audio_files_per_folder[path.parent] == 1,
                    metadata_loader=metadata_loader,
                )
            )

    return ScanResult(
        source=source,
        records=records,
        directory_errors=directory_errors,
    )
