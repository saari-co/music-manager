"""Read-only music library discovery and metadata extraction."""

from __future__ import annotations

import fnmatch
import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple
from uuid import RFC_4122, UUID, uuid4

from music_manager.artifact_schema import (
    make_file_fingerprint,
    make_file_record_id,
)
from music_manager.models import ScanFinding, ScanRecord, ScanResult
from music_manager.utils import clean_error, first_tag

try:
    import mutagen
except ImportError:  # The CLI reports the missing optional runtime dependency.
    mutagen = None


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".wav"}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | {".zip"}
MetadataLoader = Callable[[Path], Any]

_TRACK_DISC_RE = re.compile(
    r"^([0-9]+)(?:\s*(?:/|of)\s*([0-9]+))?$",
    re.IGNORECASE,
)
_RELEASE_YEAR_RE = re.compile(r"^([0-9]{4})(?:$|[-/.])")


@dataclass
class _DiscoveryResult:
    paths: List[Path] = field(default_factory=list)
    directory_errors: List[str] = field(default_factory=list)
    findings: List[ScanFinding] = field(default_factory=list)


def metadata_reader_available() -> bool:
    """Return whether Mutagen is available to read audio metadata."""
    return mutagen is not None


def _load_metadata(path: Path) -> Any:
    """Load one file with Mutagen without saving or modifying it."""
    if mutagen is None:
        raise RuntimeError("Mutagen is not installed")
    return mutagen.File(path)


def _source_relative(path: Path, source: Path) -> str:
    return path.relative_to(source).as_posix()


def _is_ignored(path: Path, source: Path, ignore_patterns: Sequence[str]) -> bool:
    """Match a path against configured source-relative ignore patterns."""
    relative_path = _source_relative(path, source)
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


def _sanitize_error(
    error: BaseException,
    source: Path,
    *,
    path: Optional[Path] = None,
    relative_path: str = "",
) -> str:
    """Remove known absolute source paths from an in-memory finding."""
    message = clean_error(error)
    replacements: list[tuple[str, str]] = []
    if path is not None:
        replacements.append((str(path), relative_path or path.name))
    replacements.append((f"{source}{os.sep}", ""))
    replacements.append((str(source), "."))
    for private, replacement in replacements:
        if private:
            message = message.replace(private, replacement)
    return message


def _symlink_finding(relative_path: str) -> ScanFinding:
    return ScanFinding(
        path=relative_path,
        stage="discovery",
        severity="info",
        error_code="symlink_skipped",
        message="Symlink was not followed",
    )


def _discover_library_entries(
    source: Path,
    ignore_patterns: Sequence[str],
) -> _DiscoveryResult:
    result = _DiscoveryResult()

    def record_walk_error(error: OSError) -> None:
        error_path: Optional[Path] = None
        relative_path = ""
        if isinstance(error.filename, str):
            error_path = Path(error.filename)
            try:
                relative_path = _source_relative(error_path, source)
            except ValueError:
                relative_path = ""
        message = _sanitize_error(
            error,
            source,
            path=error_path,
            relative_path=relative_path,
        )
        result.directory_errors.append(message)
        result.findings.append(
            ScanFinding(
                path=relative_path,
                stage="discovery",
                severity="error",
                error_code="directory_read_failed",
                message=message,
            )
        )

    for root, directories, filenames in os.walk(
        source,
        topdown=True,
        onerror=record_walk_error,
        followlinks=False,
    ):
        directories.sort(key=str.casefold)
        retained_directories: list[str] = []
        for directory in directories:
            path = Path(root) / directory
            if _is_ignored(path, source, ignore_patterns):
                continue
            if os.path.islink(path):
                result.findings.append(_symlink_finding(_source_relative(path, source)))
                continue
            retained_directories.append(directory)
        directories[:] = retained_directories

        for filename in sorted(filenames, key=str.casefold):
            path = Path(root) / filename
            if _is_ignored(path, source, ignore_patterns):
                continue
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if os.path.islink(path):
                result.findings.append(_symlink_finding(_source_relative(path, source)))
                continue
            result.paths.append(path)

    return result


def discover_files(
    source: Path, ignore_patterns: Sequence[str] = ()
) -> Tuple[List[Path], List[str]]:
    """Find regular supported files without following symlinks."""
    discovered = _discover_library_entries(source, ignore_patterns)
    return discovered.paths, discovered.directory_errors


def _validate_scan_id(scan_id: UUID) -> None:
    if scan_id.version != 4 or scan_id.variant != RFC_4122:
        raise ValueError("scan_id must be a UUIDv4")


def _new_record(
    path: Path,
    source: Path,
    scan_id: UUID,
    *,
    file_type: str,
) -> ScanRecord:
    relative_path = _source_relative(path, source)
    return ScanRecord(
        path=path,
        extension=path.suffix.lower(),
        file_type=file_type,
        scan_id=scan_id,
        file_record_id=make_file_record_id(scan_id, relative_path),
        relative_path=relative_path,
        container=path.suffix.lower().removeprefix("."),
        is_archive=file_type == "archive",
    )


def _error_finding(
    record: ScanRecord,
    error: BaseException,
    source: Path,
    *,
    stage: str,
    error_code: str,
) -> ScanFinding:
    message = _sanitize_error(
        error,
        source,
        path=record.path,
        relative_path=record.relative_path,
    )
    record.status = "error"
    record.error = message
    return ScanFinding(
        path=record.relative_path,
        stage=stage,
        severity="error",
        error_code=error_code,
        message=message,
        file_record_id=record.file_record_id,
    )


def _populate_stat(
    record: ScanRecord,
    source: Path,
) -> Optional[ScanFinding]:
    try:
        stat_result = record.path.lstat()
    except OSError as error:
        return _error_finding(
            record,
            error,
            source,
            stage="stat",
            error_code="file_stat_failed",
        )
    if stat.S_ISLNK(stat_result.st_mode):
        return _symlink_finding(record.relative_path)
    record.file_size_bytes = stat_result.st_size
    record.modified_time_ns = stat_result.st_mtime_ns
    record.file_fingerprint = make_file_fingerprint(
        record.file_size_bytes,
        record.modified_time_ns,
    )
    return None


def _parse_number_pair(value: str) -> tuple[Optional[int], Optional[int]]:
    if not value:
        return None, None
    match = _TRACK_DISC_RE.fullmatch(value)
    if match is None:
        return None, None
    current = int(match.group(1))
    total = int(match.group(2)) if match.group(2) is not None else None
    return current, total


def _parse_release_year(value: str) -> Optional[int]:
    match = _RELEASE_YEAR_RE.match(value)
    return int(match.group(1)) if match is not None else None


def _parse_tag_bool(value: str) -> Optional[bool]:
    normalized = value.casefold()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _nonnegative_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return 0.0 if parsed == 0 else parsed


def _nonnegative_int(value: Any) -> Optional[int]:
    parsed = _nonnegative_float(value)
    if parsed is None or not parsed.is_integer():
        return None
    return int(parsed)


def _first_info_text(info: Any, names: Sequence[str]) -> str:
    for name in names:
        value = getattr(info, name, None)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _populate_audio_metadata(record: ScanRecord, audio: Any) -> None:
    tags = getattr(audio, "tags", None)
    record.artist = first_tag(tags, ("artist", "TPE1", "\xa9ART"))
    record.album_artist = first_tag(
        tags,
        ("albumartist", "album artist", "TPE2", "aART"),
    )
    record.title = first_tag(tags, ("title", "TIT2", "\xa9nam"))
    record.album = first_tag(tags, ("album", "TALB", "\xa9alb"))
    record.date = first_tag(
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
    record.date_year = record.date
    record.release_year = _parse_release_year(record.date)

    raw_track = first_tag(tags, ("tracknumber", "TRCK", "trkn"))
    record.parsed_track_number, record.track_total = _parse_number_pair(raw_track)
    record.track_number = (
        "" if record.parsed_track_number is None else str(record.parsed_track_number)
    )
    raw_disc = first_tag(tags, ("discnumber", "TPOS", "disk"))
    record.disc_number, record.disc_total = _parse_number_pair(raw_disc)

    record.genre = first_tag(tags, ("genre", "TCON", "\xa9gen", "gnre"))
    record.composer = first_tag(tags, ("composer", "TCOM", "\xa9wrt"))
    record.is_compilation = _parse_tag_bool(
        first_tag(tags, ("compilation", "TCMP", "cpil"))
    )

    info = getattr(audio, "info", None)
    if info is None:
        return
    bitrate = _nonnegative_float(getattr(info, "bitrate", None))
    duration = _nonnegative_float(getattr(info, "length", None))
    if bitrate is not None:
        record.bitrate_kbps = round(bitrate / 1000, 2)
    if duration is not None:
        record.duration_seconds = round(duration, 3)
    record.sample_rate_hz = _nonnegative_int(getattr(info, "sample_rate", None))
    record.bit_depth = _nonnegative_int(
        getattr(info, "bits_per_sample", getattr(info, "bit_depth", None))
    )
    record.channels = _nonnegative_int(getattr(info, "channels", None))
    record.codec = _first_info_text(
        info,
        ("codec", "codec_name", "codec_description"),
    )


def _scan_audio_file(
    path: Path,
    source: Path,
    scan_id: UUID,
    metadata_loader: Optional[MetadataLoader],
) -> tuple[Optional[ScanRecord], List[ScanFinding]]:
    record = _new_record(path, source, scan_id, file_type="audio")
    findings: List[ScanFinding] = []
    stat_finding = _populate_stat(record, source)
    if stat_finding is not None:
        findings.append(stat_finding)
        if stat_finding.error_code == "symlink_skipped":
            return None, findings
        return record, findings

    try:
        loader = metadata_loader or _load_metadata
        audio = loader(path)
        if audio is None:
            raise ValueError("Mutagen could not identify the audio format")
        _populate_audio_metadata(record, audio)
    except Exception as error:
        findings.append(
            _error_finding(
                record,
                error,
                source,
                stage="metadata",
                error_code="metadata_read_failed",
            )
        )
    return record, findings


def scan_audio_file(
    path: Path,
    metadata_loader: Optional[MetadataLoader] = None,
    *,
    source: Optional[Path] = None,
    scan_id: Optional[UUID] = None,
) -> ScanRecord:
    """Read one regular audio file without writing or following symlinks."""
    if os.path.islink(path):
        raise ValueError("symlinked audio files are not scanned")
    selected_source = source or path.parent
    selected_scan_id = scan_id or uuid4()
    _validate_scan_id(selected_scan_id)
    record, _findings = _scan_audio_file(
        path,
        selected_source,
        selected_scan_id,
        metadata_loader,
    )
    if record is None:
        raise ValueError("symlinked audio files are not scanned")
    return record


def _scan_archive(
    path: Path,
    source: Path,
    scan_id: UUID,
) -> tuple[Optional[ScanRecord], List[ScanFinding]]:
    record = _new_record(path, source, scan_id, file_type="archive")
    finding = _populate_stat(record, source)
    if finding is not None and finding.error_code == "symlink_skipped":
        return None, [finding]
    return record, [] if finding is None else [finding]


def scan_archive(
    path: Path,
    *,
    source: Optional[Path] = None,
    scan_id: Optional[UUID] = None,
) -> ScanRecord:
    """Record one regular ZIP archive without opening or extracting it."""
    if os.path.islink(path):
        raise ValueError("symlinked archives are not scanned")
    selected_source = source or path.parent
    selected_scan_id = scan_id or uuid4()
    _validate_scan_id(selected_scan_id)
    record, _findings = _scan_archive(
        path,
        selected_source,
        selected_scan_id,
    )
    if record is None:
        raise ValueError("symlinked archives are not scanned")
    return record


def scan_library(
    source: Path,
    metadata_loader: Optional[MetadataLoader] = None,
    ignore_patterns: Sequence[str] = (),
    *,
    scan_id: Optional[UUID] = None,
) -> ScanResult:
    """Scan a source tree into schema-ready records without modifying it."""
    selected_scan_id = scan_id or uuid4()
    _validate_scan_id(selected_scan_id)
    discovered = _discover_library_entries(source, ignore_patterns)
    records: List[ScanRecord] = []
    findings = list(discovered.findings)

    for path in discovered.paths:
        if path.suffix.lower() == ".zip":
            record, record_findings = _scan_archive(
                path,
                source,
                selected_scan_id,
            )
        else:
            record, record_findings = _scan_audio_file(
                path,
                source,
                selected_scan_id,
                metadata_loader,
            )
        if record is not None:
            records.append(record)
        findings.extend(record_findings)

    return ScanResult(
        source=source,
        records=records,
        directory_errors=discovered.directory_errors,
        scan_id=selected_scan_id,
        findings=findings,
    )
