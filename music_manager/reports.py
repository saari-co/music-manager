"""Report writers for Music Manager scan and analysis results."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from music_manager.analyzer import QUALITY_BUCKETS
from music_manager.config import PATH_MODES
from music_manager.models import CsvValue, LibraryAnalysis, ScanRecord
from music_manager.sources import source_name_for_relative_path


CSV_FIELDNAMES = [
    "path",
    "extension",
    "file_type",
    "library_source",
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
LEGACY_SCAN_FIELDNAMES = [
    field_name
    for field_name in CSV_FIELDNAMES
    if field_name != "library_source"
]
ANALYSIS_FIELDNAMES = ["metric", "value"]
DUPLICATE_FIELDNAMES = [
    "duplicate_group_id",
    "library_source",
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
    "library_source",
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
    "library_source",
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
FOLDER_FIELDNAMES = [
    "record_type",
    "library_source",
    "folder_depth",
    "file_count",
    "path",
    "is_loose_track",
    "is_extreme_nesting",
]
ANALYSIS_REPORT_FILENAMES = {
    "analysis": "library_analysis.csv",
    "duplicates": "duplicate_candidates.csv",
    "missing_metadata": "missing_metadata.csv",
    "corrupt_files": "corrupt_files.csv",
    "quality": "quality_summary.csv",
    "folders": "folder_summary.csv",
}


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


def _source_name(path: Path, fallback: str = "Source") -> str:
    return source_name_for_relative_path(path, fallback=fallback)


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
            row["library_source"] = record.library_source or _source_name(
                reported_path,
                fallback=source.name if source is not None else "Source",
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


def read_scan_report(
    report_path: Path, path_mode: str = "relative"
) -> List[ScanRecord]:
    """Load scan rows without opening any music paths referenced by the CSV."""
    _validate_path_mode(path_mode)
    with report_path.open(encoding="utf-8", newline="") as report_file:
        reader = csv.DictReader(report_file)
        fieldnames = set(reader.fieldnames or [])
        missing_columns = [
            field_name
            for field_name in LEGACY_SCAN_FIELDNAMES
            if field_name not in fieldnames
        ]
        if missing_columns:
            missing_list = ", ".join(missing_columns)
            raise ValueError(f"scan report is missing columns: {missing_list}")
        records = [ScanRecord.from_csv_row(row) for row in reader]

    relative_base = _common_absolute_root(records)
    fallback_source = (
        relative_base.name if relative_base is not None else "Source"
    )
    for record in records:
        original_path = record.path
        source_path = _relative_report_path(record.path, relative_base)
        if not record.library_source:
            record.library_source = _source_name(
                source_path, fallback=fallback_source
            )
        if path_mode == "relative":
            record.path = source_path
            record.error = _relative_error(
                record.error,
                original_path,
                source_path,
                relative_base,
            )
    return records


def _analysis_rows(analysis: LibraryAnalysis) -> Iterable[Mapping[str, CsvValue]]:
    summary = analysis.summary
    metrics = (
        ("total_audio_files", summary.total_audio_files),
        ("duplicate_candidate_groups", summary.duplicate_candidate_groups),
        ("duplicate_candidate_files", summary.duplicate_candidate_files),
        ("files_with_missing_metadata", summary.files_with_missing_metadata),
        ("corrupt_or_unreadable_files", summary.corrupt_or_unreadable_files),
        ("low_bitrate_files", summary.low_bitrate_files),
        ("loose_tracks", summary.loose_tracks),
        ("deepest_folder_depth", summary.deepest_folder_depth),
        ("extreme_nesting_files", summary.extreme_nesting_files),
        ("library_sources", summary.library_source_count),
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
                "library_source": record.library_source,
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
            "library_source": record.library_source,
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
            "library_source": record.library_source,
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


def _folder_rows(analysis: LibraryAnalysis) -> Iterable[Mapping[str, CsvValue]]:
    for depth, count in analysis.folder_depth_counts.items():
        yield {
            "record_type": "depth_distribution",
            "library_source": "",
            "folder_depth": depth,
            "file_count": count,
            "path": "",
            "is_loose_track": "",
            "is_extreme_nesting": "",
        }

    yield {
        "record_type": "loose_track_summary",
        "library_source": "",
        "folder_depth": "",
        "file_count": analysis.summary.loose_tracks,
        "path": "",
        "is_loose_track": "",
        "is_extreme_nesting": "",
    }

    for library_source, count in analysis.library_source_counts.items():
        yield {
            "record_type": "library_source_summary",
            "library_source": library_source,
            "folder_depth": "",
            "file_count": count,
            "path": "",
            "is_loose_track": "",
            "is_extreme_nesting": "",
        }

    extreme_paths = {record.path for record in analysis.extreme_nesting_files}
    for record in analysis.deepest_files:
        yield {
            "record_type": "deepest_file",
            "library_source": record.library_source,
            "folder_depth": record.folder_depth,
            "file_count": "",
            "path": str(record.path),
            "is_loose_track": record.is_loose_track,
            "is_extreme_nesting": record.path in extreme_paths,
        }

    for record in analysis.extreme_nesting_files:
        yield {
            "record_type": "extreme_nesting_file",
            "library_source": record.library_source,
            "folder_depth": record.folder_depth,
            "file_count": "",
            "path": str(record.path),
            "is_loose_track": record.is_loose_track,
            "is_extreme_nesting": True,
        }


def write_analysis_reports(
    analysis: LibraryAnalysis, output_directory: Path
) -> Dict[str, Path]:
    """Write all v0.2 analysis reports and return their paths."""
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
        (paths["folders"], FOLDER_FIELDNAMES, _folder_rows(analysis)),
    )
    for report_path, fieldnames, rows in report_specs:
        _write_rows(report_path, fieldnames, rows)
    return paths
