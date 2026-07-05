"""Report writers for Music Manager scan and analysis results."""

from __future__ import annotations

import csv
import math
import ntpath
import os
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import Dict, Iterable, List, Mapping, Optional, Sequence
from uuid import UUID

from music_manager.analyzer import QUALITY_BUCKETS
from music_manager.config import PATH_MODES
from music_manager.models import CsvValue, LibraryAnalysis, ScanRecord


CSV_FIELDNAMES = [
    "path",
    "extension",
    "file_type",
    "file_size_bytes",
    "artist",
    "title",
    "album",
    "date_year",
    "track_number",
    "bitrate_kbps",
    "duration_seconds",
    "is_archive",
    "status",
    "error",
]
LEGACY_SCAN_FIELDNAMES = [
    "path",
    "extension",
    "file_type",
    "file_size_bytes",
    "folder_depth",
    "artist",
    "title",
    "album",
    "date_year",
    "track_number",
    "bitrate_kbps",
    "duration_seconds",
    "is_loose_track",
    "is_archive",
    "status",
    "error",
]
SUPPORTED_LEGACY_SCAN_HEADERS = (
    tuple(CSV_FIELDNAMES),
    tuple(LEGACY_SCAN_FIELDNAMES),
)
LEGACY_FILE_TYPES = frozenset({"audio", "archive"})
LEGACY_RECORD_STATUSES = frozenset({"ok", "error"})
ANALYSIS_FIELDNAMES = ["metric", "value"]
DUPLICATE_FIELDNAMES = [
    "duplicate_group_id",
    "path",
    "artist",
    "title",
    "album",
    "date_year",
    "track_number",
    "bitrate_kbps",
    "duration_seconds",
    "file_size_bytes",
]
MISSING_METADATA_FIELDNAMES = [
    "path",
    "extension",
    "file_type",
    "artist",
    "title",
    "album",
    "date_year",
    "track_number",
    "missing_fields",
]
CORRUPT_FILE_FIELDNAMES = [
    "path",
    "extension",
    "file_type",
    "status",
    "error",
]
QUALITY_FIELDNAMES = [
    "quality_bucket",
    "file_count",
    "is_suspicious_low_quality",
]
LIBRARY_ANALYSIS_HEADER = ("scan_id", *ANALYSIS_FIELDNAMES)
DUPLICATE_CANDIDATES_HEADER = (
    "scan_id",
    "file_record_id",
    *DUPLICATE_FIELDNAMES,
)
MISSING_METADATA_HEADER = (
    "scan_id",
    "file_record_id",
    *MISSING_METADATA_FIELDNAMES,
)
CORRUPT_FILES_HEADER = (
    "scan_id",
    "file_record_id",
    *CORRUPT_FILE_FIELDNAMES,
)
QUALITY_SUMMARY_HEADER = ("scan_id", *QUALITY_FIELDNAMES)
ANALYSIS_REPORT_FILENAMES = {
    "analysis": "library_analysis.csv",
    "duplicates": "duplicate_candidates.csv",
    "missing_metadata": "missing_metadata.csv",
    "corrupt_files": "corrupt_files.csv",
    "quality": "quality_summary.csv",
}
VERSIONED_ANALYSIS_REPORT_FILENAMES = {
    "library_analysis": "library_analysis.csv",
    "duplicate_candidates": "duplicate_candidates.csv",
    "missing_metadata": "missing_metadata.csv",
    "corrupt_files": "corrupt_files.csv",
    "quality_summary": "quality_summary.csv",
}


class LegacyReportValidationError(ValueError):
    """Raised when an unversioned v0.2 scan report is malformed."""


@dataclass(frozen=True)
class AnalysisReportSpec:
    """One versioned analysis report ready for transactional staging."""

    logical_name: str
    filename: str
    fieldnames: Sequence[str]
    rows: Iterable[Mapping[str, CsvValue]]


def _write_rows(
    report_path: Path,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping[str, CsvValue]],
) -> None:
    """Write dictionaries to one UTF-8 CSV report."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _validate_path_mode(path_mode: str) -> None:
    if path_mode not in PATH_MODES:
        choices = ", ".join(sorted(PATH_MODES))
        raise ValueError(f"path mode must be one of: {choices}")


def _legacy_error(location: str, message: str) -> LegacyReportValidationError:
    return LegacyReportValidationError(f"{location}: {message}")


def _validate_legacy_header(fieldnames: Sequence[str]) -> tuple[str, ...]:
    header = tuple(fieldnames)
    duplicates = sorted(name for name, count in Counter(header).items() if count > 1)
    if duplicates:
        raise _legacy_error(
            "legacy scan header",
            f"duplicate columns: {', '.join(duplicates)}",
        )
    if header in SUPPORTED_LEGACY_SCAN_HEADERS:
        return header

    allowed_columns = set().union(*SUPPORTED_LEGACY_SCAN_HEADERS)
    unknown = [name for name in header if name not in allowed_columns]
    if unknown:
        raise _legacy_error(
            "legacy scan header",
            f"unknown columns: {', '.join(unknown)}",
        )

    extended_only = {"folder_depth", "is_loose_track"}
    expected = (
        tuple(LEGACY_SCAN_FIELDNAMES)
        if extended_only.intersection(header)
        else tuple(CSV_FIELDNAMES)
    )
    missing = [name for name in expected if name not in header]
    if missing:
        raise _legacy_error(
            "legacy scan header",
            f"missing required columns: {', '.join(missing)}",
        )
    raise _legacy_error(
        "legacy scan header",
        "must exactly match a documented v0.2 header, including column order",
    )


def _legacy_required_text(
    row: Mapping[str, str],
    field_name: str,
    location: str,
) -> str:
    value = row[field_name]
    if not value.strip():
        raise _legacy_error(location, f"{field_name} must not be empty")
    return value


def _legacy_optional_integer(
    row: Mapping[str, str],
    field_name: str,
    location: str,
    *,
    required: bool = False,
) -> Optional[int]:
    value = row[field_name].strip()
    if not value:
        if required:
            raise _legacy_error(location, f"{field_name} must not be empty")
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise _legacy_error(
            location,
            f"{field_name} must be a non-negative integer",
        ) from error
    if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
        raise _legacy_error(
            location,
            f"{field_name} must be a non-negative integer",
        )
    try:
        return int(parsed)
    except (OverflowError, ValueError) as error:
        raise _legacy_error(
            location,
            f"{field_name} must be a non-negative integer",
        ) from error


def _legacy_optional_decimal(
    row: Mapping[str, str],
    field_name: str,
    location: str,
) -> Optional[float]:
    value = row[field_name].strip()
    if not value:
        return None
    try:
        decimal_value = Decimal(value)
        parsed = float(decimal_value)
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise _legacy_error(
            location,
            f"{field_name} must be a finite non-negative decimal",
        ) from error
    if not decimal_value.is_finite() or decimal_value < 0 or not math.isfinite(parsed):
        raise _legacy_error(
            location,
            f"{field_name} must be a finite non-negative decimal",
        )
    return parsed


def _legacy_boolean(
    row: Mapping[str, str],
    field_name: str,
    location: str,
    *,
    nullable: bool = False,
) -> bool:
    value = row[field_name].strip().casefold()
    if not value and nullable:
        return False
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise _legacy_error(
        location,
        f"{field_name} must be a legacy boolean",
    )


def _legacy_choice(
    row: Mapping[str, str],
    field_name: str,
    choices: frozenset[str],
    location: str,
) -> str:
    value = row[field_name].strip().casefold()
    if value not in choices:
        expected = ", ".join(sorted(choices))
        raise _legacy_error(
            location,
            f"{field_name} must be one of: {expected}",
        )
    return value


def _legacy_record(
    row: Mapping[str, str],
    header: Sequence[str],
    location: str,
) -> ScanRecord:
    for field_name, value in row.items():
        if "\x00" in value:
            raise _legacy_error(location, f"{field_name} must not contain NUL")

    path = _legacy_required_text(row, "path", location)
    extension = _legacy_required_text(row, "extension", location).strip().lower()
    file_type = _legacy_choice(
        row,
        "file_type",
        LEGACY_FILE_TYPES,
        location,
    )
    status = _legacy_choice(
        row,
        "status",
        LEGACY_RECORD_STATUSES,
        location,
    )
    file_size_bytes = _legacy_optional_integer(
        row,
        "file_size_bytes",
        location,
    )
    bitrate_kbps = _legacy_optional_decimal(
        row,
        "bitrate_kbps",
        location,
    )
    duration_seconds = _legacy_optional_decimal(
        row,
        "duration_seconds",
        location,
    )
    is_archive = _legacy_boolean(
        row,
        "is_archive",
        location,
        nullable=True,
    )

    if "folder_depth" in header:
        _legacy_optional_integer(
            row,
            "folder_depth",
            location,
        )
        _legacy_boolean(
            row,
            "is_loose_track",
            location,
            nullable=True,
        )

    return ScanRecord(
        path=Path(path),
        extension=extension,
        file_type=file_type,
        file_size_bytes=file_size_bytes,
        artist=row["artist"].strip(),
        title=row["title"].strip(),
        album=row["album"].strip(),
        date_year=row["date_year"].strip(),
        track_number=row["track_number"].strip(),
        bitrate_kbps=bitrate_kbps,
        duration_seconds=duration_seconds,
        is_archive=is_archive,
        status=status,
        error=row["error"].strip(),
    )


def _common_absolute_root(records: Sequence[ScanRecord]) -> Optional[Path]:
    absolute_paths = [record.path for record in records if record.path.is_absolute()]
    if not absolute_paths:
        return None
    try:
        common_root = Path(os.path.commonpath([str(path) for path in absolute_paths]))
    except ValueError:
        return None
    if all(path == common_root for path in absolute_paths):
        return common_root.parent
    return common_root


def _relative_report_path(path: Path, base: Optional[Path]) -> Path:
    if not path.is_absolute():
        return path
    if base is not None and base != Path(path.anchor):
        try:
            return path.relative_to(base)
        except ValueError:
            pass
    try:
        return path.relative_to(Path.home())
    except ValueError:
        return path.relative_to(Path(path.anchor))


def _relative_error(
    error: str,
    original_path: Path,
    reported_path: Path,
    base: Optional[Path],
) -> str:
    """Remove known absolute path prefixes from a reported error message."""
    if not error:
        return error
    sanitized = error.replace(str(original_path), str(reported_path))
    for prefix in (base, Path.home()):
        if prefix is None or prefix == Path(prefix.anchor):
            continue
        prefix_text = str(prefix)
        sanitized = sanitized.replace(f"{prefix_text}{os.sep}", "")
        sanitized = sanitized.replace(prefix_text, "")
    return sanitized


def _windows_absolute_path(path: Path) -> Optional[PureWindowsPath]:
    if path.is_absolute():
        return None
    windows_path = PureWindowsPath(str(path))
    return windows_path if windows_path.is_absolute() else None


def _common_windows_root(
    paths: Sequence[PureWindowsPath],
) -> Optional[PureWindowsPath]:
    if not paths:
        return None
    try:
        common = PureWindowsPath(ntpath.commonpath([str(path) for path in paths]))
    except ValueError:
        return None
    if all(path == common for path in paths):
        return common.parent
    return common


def _relative_windows_path(
    path: PureWindowsPath,
    base: Optional[PureWindowsPath],
) -> Path:
    if base is not None and base != PureWindowsPath(path.anchor):
        try:
            return Path(path.relative_to(base).as_posix())
        except ValueError:
            pass
    return Path(path.relative_to(path.anchor).as_posix())


def _relative_windows_error(
    error: str,
    original_path: PureWindowsPath,
    reported_path: Path,
    base: Optional[PureWindowsPath],
) -> str:
    if not error:
        return error
    sanitized = error.replace(str(original_path), str(reported_path))
    if base is not None and base != PureWindowsPath(base.anchor):
        base_text = str(base)
        sanitized = sanitized.replace(f"{base_text}\\", "")
        sanitized = sanitized.replace(base_text, "")
    return sanitized


def _relativize_windows_records(records: Sequence[ScanRecord]) -> None:
    windows_paths = [
        windows_path
        for record in records
        if (windows_path := _windows_absolute_path(record.path)) is not None
    ]
    base = _common_windows_root(windows_paths)
    for record in records:
        original_path = _windows_absolute_path(record.path)
        if original_path is None:
            continue
        reported_path = _relative_windows_path(original_path, base)
        record.path = reported_path
        record.error = _relative_windows_error(
            record.error,
            original_path,
            reported_path,
            base,
        )


def _write_scan_report(
    records: Sequence[ScanRecord],
    report_path: Path,
    *,
    source: Optional[Path] = None,
    path_mode: str = "relative",
) -> None:
    _validate_path_mode(path_mode)
    relative_base = source or _common_absolute_root(records)

    def rows() -> Iterable[Mapping[str, CsvValue]]:
        for record in records:
            reported_path = (
                record.path
                if path_mode == "absolute"
                else _relative_report_path(record.path, relative_base)
            )
            row = record.to_csv_row()
            row["path"] = str(reported_path)
            if path_mode == "relative":
                row["error"] = _relative_error(
                    record.error,
                    record.path,
                    reported_path,
                    relative_base,
                )
            yield row

    _write_rows(
        report_path,
        CSV_FIELDNAMES,
        rows(),
    )


def write_csv_report(
    records: Sequence[ScanRecord],
    report_path: Path,
    *,
    source: Optional[Path] = None,
    path_mode: str = "relative",
) -> None:
    """Write scan records with privacy-friendly paths by default."""
    _write_scan_report(
        records,
        report_path,
        source=source,
        path_mode=path_mode,
    )


def read_legacy_scan_report(
    report_path: Path,
    path_mode: str = "relative",
) -> List[ScanRecord]:
    """Strictly load one unversioned v0.2 report without accessing music paths."""
    _validate_path_mode(path_mode)
    manifest_path = report_path.parent / "scan_manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        raise LegacyReportValidationError(
            "legacy compatibility mode refuses a report with a sibling "
            "scan_manifest.json; use --scan-run for schema 1 analysis"
        )

    with report_path.open(encoding="utf-8", newline="") as report_file:
        reader = csv.reader(report_file, strict=True)
        try:
            header = _validate_legacy_header(next(reader))
        except StopIteration as error:
            raise _legacy_error(
                "legacy scan header",
                "report is empty",
            ) from error

        records: list[ScanRecord] = []
        for cells in reader:
            location = f"legacy scan row {reader.line_num}"
            if len(cells) != len(header):
                raise _legacy_error(
                    location,
                    f"expected {len(header)} columns, found {len(cells)}",
                )
            records.append(
                _legacy_record(
                    dict(zip(header, cells, strict=True)),
                    header,
                    location,
                )
            )

    if path_mode == "relative":
        _relativize_windows_records(records)
    relative_base = _common_absolute_root(records)
    for record in records:
        original_path = record.path
        source_path = _relative_report_path(record.path, relative_base)
        if path_mode == "relative":
            record.path = source_path
            record.error = _relative_error(
                record.error,
                original_path,
                source_path,
                relative_base,
            )
    return records


def read_scan_report(
    report_path: Path,
    path_mode: str = "relative",
) -> List[ScanRecord]:
    """Compatibility alias for the strict legacy v0.2 report reader."""
    return read_legacy_scan_report(report_path, path_mode=path_mode)


def _analysis_rows(analysis: LibraryAnalysis) -> Iterable[Mapping[str, CsvValue]]:
    summary = analysis.summary
    metrics = (
        ("root_library_total", summary.root_library_total),
        ("duplicate_candidate_groups", summary.duplicate_candidate_groups),
        ("duplicate_candidate_files", summary.duplicate_candidate_files),
        ("files_with_missing_metadata", summary.files_with_missing_metadata),
        ("corrupt_or_unreadable_files", summary.corrupt_or_unreadable_files),
        ("low_bitrate_files", summary.low_bitrate_files),
    )
    metadata_metrics = tuple(
        (f"{field_name}_complete_percent", percentage)
        for field_name, percentage in analysis.metadata_completeness.items()
    )
    return (
        {"metric": metric, "value": value}
        for metric, value in metrics + metadata_metrics
    )


def _duplicate_rows(analysis: LibraryAnalysis) -> Iterable[Mapping[str, CsvValue]]:
    for group in analysis.duplicate_groups:
        for record in group.records:
            yield {
                "duplicate_group_id": group.group_id,
                "path": str(record.path),
                "artist": record.artist,
                "title": record.title,
                "album": record.album,
                "date_year": record.date_year,
                "track_number": record.track_number,
                "bitrate_kbps": record.bitrate_kbps
                if record.bitrate_kbps is not None
                else "",
                "duration_seconds": record.duration_seconds
                if record.duration_seconds is not None
                else "",
                "file_size_bytes": record.file_size_bytes
                if record.file_size_bytes is not None
                else "",
            }


def _missing_metadata_rows(
    analysis: LibraryAnalysis,
) -> Iterable[Mapping[str, CsvValue]]:
    for finding in analysis.missing_metadata:
        record = finding.record
        yield {
            "path": str(record.path),
            "extension": record.extension,
            "file_type": record.file_type,
            "artist": record.artist,
            "title": record.title,
            "album": record.album,
            "date_year": record.date_year,
            "track_number": record.track_number,
            "missing_fields": ";".join(finding.missing_fields),
        }


def _corrupt_file_rows(
    analysis: LibraryAnalysis,
) -> Iterable[Mapping[str, CsvValue]]:
    for record in analysis.corrupt_files:
        yield {
            "path": str(record.path),
            "extension": record.extension,
            "file_type": record.file_type,
            "status": record.status,
            "error": record.error,
        }


def _quality_rows(analysis: LibraryAnalysis) -> Iterable[Mapping[str, CsvValue]]:
    for bucket in QUALITY_BUCKETS:
        yield {
            "quality_bucket": bucket,
            "file_count": analysis.quality_buckets[bucket],
            "is_suspicious_low_quality": bucket == "under_128",
        }


def _with_scan_id(
    rows: Iterable[Mapping[str, CsvValue]],
    scan_id: UUID,
) -> Iterable[Mapping[str, CsvValue]]:
    scan_id_text = str(scan_id)
    for row in rows:
        yield {"scan_id": scan_id_text, **row}


def _with_file_provenance(
    rows: Iterable[Mapping[str, CsvValue]],
    records: Iterable[ScanRecord],
    scan_id: UUID,
) -> Iterable[Mapping[str, CsvValue]]:
    scan_id_text = str(scan_id)
    for row, record in zip(rows, records, strict=True):
        if record.file_record_id is None:
            raise ValueError(f"analysis record {record.path!s} has no file_record_id")
        yield {
            "scan_id": scan_id_text,
            "file_record_id": str(record.file_record_id),
            **row,
        }


def _duplicate_records(analysis: LibraryAnalysis) -> Iterable[ScanRecord]:
    for group in analysis.duplicate_groups:
        yield from group.records


def _missing_metadata_records(
    analysis: LibraryAnalysis,
) -> Iterable[ScanRecord]:
    return (finding.record for finding in analysis.missing_metadata)


def _validate_analysis_provenance(
    analysis: LibraryAnalysis,
    scan_id: UUID,
) -> None:
    for record in analysis.records:
        if record.scan_id != scan_id:
            raise ValueError(
                f"analysis record {record.path!s} has a mismatched scan_id"
            )
        if record.file_record_id is None:
            raise ValueError(f"analysis record {record.path!s} has no file_record_id")


def versioned_analysis_report_specs(
    analysis: LibraryAnalysis,
    scan_id: UUID,
) -> tuple[AnalysisReportSpec, ...]:
    """Build schema 1 report specifications with scan-local provenance."""
    _validate_analysis_provenance(analysis, scan_id)
    return (
        AnalysisReportSpec(
            "library_analysis",
            VERSIONED_ANALYSIS_REPORT_FILENAMES["library_analysis"],
            LIBRARY_ANALYSIS_HEADER,
            _with_scan_id(_analysis_rows(analysis), scan_id),
        ),
        AnalysisReportSpec(
            "duplicate_candidates",
            VERSIONED_ANALYSIS_REPORT_FILENAMES["duplicate_candidates"],
            DUPLICATE_CANDIDATES_HEADER,
            _with_file_provenance(
                _duplicate_rows(analysis),
                _duplicate_records(analysis),
                scan_id,
            ),
        ),
        AnalysisReportSpec(
            "missing_metadata",
            VERSIONED_ANALYSIS_REPORT_FILENAMES["missing_metadata"],
            MISSING_METADATA_HEADER,
            _with_file_provenance(
                _missing_metadata_rows(analysis),
                _missing_metadata_records(analysis),
                scan_id,
            ),
        ),
        AnalysisReportSpec(
            "corrupt_files",
            VERSIONED_ANALYSIS_REPORT_FILENAMES["corrupt_files"],
            CORRUPT_FILES_HEADER,
            _with_file_provenance(
                _corrupt_file_rows(analysis),
                analysis.corrupt_files,
                scan_id,
            ),
        ),
        AnalysisReportSpec(
            "quality_summary",
            VERSIONED_ANALYSIS_REPORT_FILENAMES["quality_summary"],
            QUALITY_SUMMARY_HEADER,
            _with_scan_id(_quality_rows(analysis), scan_id),
        ),
    )


def write_legacy_analysis_reports(
    analysis: LibraryAnalysis, output_directory: Path
) -> Dict[str, Path]:
    """Write flat v0.2 analysis reports without schema 1 provenance."""
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = {
        name: output_directory / filename
        for name, filename in ANALYSIS_REPORT_FILENAMES.items()
    }
    report_specs = (
        (paths["analysis"], ANALYSIS_FIELDNAMES, _analysis_rows(analysis)),
        (paths["duplicates"], DUPLICATE_FIELDNAMES, _duplicate_rows(analysis)),
        (
            paths["missing_metadata"],
            MISSING_METADATA_FIELDNAMES,
            _missing_metadata_rows(analysis),
        ),
        (
            paths["corrupt_files"],
            CORRUPT_FILE_FIELDNAMES,
            _corrupt_file_rows(analysis),
        ),
        (paths["quality"], QUALITY_FIELDNAMES, _quality_rows(analysis)),
    )
    for report_path, fieldnames, rows in report_specs:
        _write_rows(report_path, fieldnames, rows)
    return paths


def write_analysis_reports(
    analysis: LibraryAnalysis, output_directory: Path
) -> Dict[str, Path]:
    """Compatibility alias for the flat v0.2 analysis report writer."""
    return write_legacy_analysis_reports(analysis, output_directory)
