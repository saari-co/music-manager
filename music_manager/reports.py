"""Report writers for Music Manager scan and analysis results."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Sequence

from music_manager.models import ScanRecord


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


def write_csv_report(records: Sequence[ScanRecord], report_path: Path) -> None:
    """Write scan records to a UTF-8 CSV file."""
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        writer.writerows(record.to_csv_row() for record in records)
