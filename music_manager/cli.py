"""Command-line interface for Music Manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from music_manager.models import ScanResult
from music_manager.reports import write_csv_report
from music_manager.scanner import metadata_reader_available, scan_library
from music_manager.utils import clean_error


DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parent.parent / "reports" / "library_scan.csv"
)


def build_parser() -> argparse.ArgumentParser:
    """Build the Music Manager argument parser."""
    parser = argparse.ArgumentParser(
        prog="music-manager",
        description=(
            "Scan a music folder in read-only mode and write metadata to "
            "reports/library_scan.csv."
        ),
        epilog="Source files are never renamed, moved, copied, deleted, or edited.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="local music folder to scan recursively (read-only)",
    )
    return parser


def _print_warnings(result: ScanResult) -> None:
    """Print non-fatal scan errors after all readable files are processed."""
    for record in result.records:
        if record.status == "error":
            print(
                f"Warning: could not read {record.path}: {record.error}",
                file=sys.stderr,
            )
    for error in result.directory_errors:
        print(f"Warning: could not scan directory: {error}", file=sys.stderr)


def _print_summary(result: ScanResult, report_path: Path) -> None:
    """Print a concise terminal summary."""
    summary = result.summary
    print("Scan complete")
    print(f"Source: {result.source}")
    print(f"Report: {report_path}")
    print(f"Audio files: {summary.audio_count}")
    print(f"Archives: {summary.archive_count}")
    print(f"Loose tracks: {summary.loose_track_count}")
    print(f"File errors: {summary.file_error_count}")
    print(f"Directory errors: {summary.directory_error_count}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the command-line application and return a process exit code."""
    args = build_parser().parse_args(argv)
    source = Path(args.source).expanduser().resolve()

    if not source.exists():
        print(f"Error: source does not exist: {source}", file=sys.stderr)
        return 2
    if not source.is_dir():
        print(f"Error: source is not a directory: {source}", file=sys.stderr)
        return 2
    if not metadata_reader_available():
        print(
            "Error: mutagen is required. Install it with: "
            "python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    result = scan_library(source)
    _print_warnings(result)

    try:
        write_csv_report(result.records, DEFAULT_REPORT_PATH)
    except OSError as error:
        print(
            f"Error: could not write report {DEFAULT_REPORT_PATH}: "
            f"{clean_error(error)}",
            file=sys.stderr,
        )
        return 1

    _print_summary(result, DEFAULT_REPORT_PATH)
    return 0
