"""Report writers for Music Manager scan and analysis results."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence

from music_manager.analyzer import QUALITY_BUCKETS
from music_manager.models import CsvValue, LibraryAnalysis, ScanRecord


CSV_FIELDNAMES = [
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
FOLDER_FIELDNAMES = [
    "record_type",
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


def write_csv_report(records: Sequence[ScanRecord], report_path: Path) -> None:
    """Write scan records to a UTF-8 CSV file."""
    _write_rows(
        report_path,
        CSV_FIELDNAMES,
        (record.to_csv_row() for record in records),
    )


def read_scan_report(report_path: Path) -> List[ScanRecord]:
    """Load scan rows without opening any music paths referenced by the CSV."""
    with report_path.open(encoding="utf-8", newline="") as report_file:
        reader = csv.DictReader(report_file)
        fieldnames = set(reader.fieldnames or [])
        missing_columns = [
            field_name
            for field_name in CSV_FIELDNAMES
            if field_name not in fieldnames
        ]
        if missing_columns:
            missing_list = ", ".join(missing_columns)
            raise ValueError(f"scan report is missing columns: {missing_list}")
        return [ScanRecord.from_csv_row(row) for row in reader]


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
    )
    return ({"metric": metric, "value": value} for metric, value in metrics)


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


def _folder_rows(analysis: LibraryAnalysis) -> Iterable[Mapping[str, CsvValue]]:
    for depth, count in analysis.folder_depth_counts.items():
        yield {
            "record_type": "depth_distribution",
            "folder_depth": depth,
            "file_count": count,
            "path": "",
            "is_loose_track": "",
            "is_extreme_nesting": "",
        }

    yield {
        "record_type": "loose_track_summary",
        "folder_depth": "",
        "file_count": analysis.summary.loose_tracks,
        "path": "",
        "is_loose_track": "",
        "is_extreme_nesting": "",
    }

    extreme_paths = {record.path for record in analysis.extreme_nesting_files}
    for record in analysis.deepest_files:
        yield {
            "record_type": "deepest_file",
            "folder_depth": record.folder_depth,
            "file_count": "",
            "path": str(record.path),
            "is_loose_track": record.is_loose_track,
            "is_extreme_nesting": record.path in extreme_paths,
        }

    for record in analysis.extreme_nesting_files:
        yield {
            "record_type": "extreme_nesting_file",
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
