"""Command-line interface for scanning and analyzing music libraries."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional, Sequence

from music_manager.analyzer import (
    DEFAULT_DURATION_TOLERANCE,
    analyze_library,
)
from music_manager.config import AppConfig, PATH_MODES, load_config
from music_manager.models import LibraryAnalysis, ScanResult
from music_manager.reports import (
    read_scan_report,
    write_analysis_reports,
)
from music_manager.scan_runs import ScanRunOutcome, create_scan_run
from music_manager.scanner import metadata_reader_available
from music_manager.utils import clean_error


DEFAULT_REPORT_DIRECTORY = Path(__file__).resolve().parent.parent / "reports"
DEFAULT_SCAN_REPORT_PATH = DEFAULT_REPORT_DIRECTORY / "library_scan.csv"


def _non_negative_float(value: str) -> float:
    """Parse a non-negative CLI float."""
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def _add_scan_arguments(parser: argparse.ArgumentParser, required: bool) -> None:
    parser.add_argument(
        "--source",
        required=required,
        help="local music folder to scan recursively (read-only)",
    )


def _add_config_arguments(
    parser: argparse.ArgumentParser, suppress_defaults: bool = False
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--config",
        type=Path,
        default=default,
        help="YAML config path (default: ./music-manager.yml when present)",
    )
    parser.add_argument(
        "--path-mode",
        choices=sorted(PATH_MODES),
        default=default,
        help="path style for generated reports (default: relative)",
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the Music Manager argument parser."""
    parser = argparse.ArgumentParser(
        prog="music-manager",
        description="Scan music libraries and analyze existing scan reports.",
        epilog="Music files are never renamed, moved, copied, deleted, or edited.",
    )
    _add_scan_arguments(parser, required=False)
    _add_config_arguments(parser)
    subparsers = parser.add_subparsers(dest="command")

    scan_parser = subparsers.add_parser(
        "scan",
        help="create a read-only CSV inventory of a music folder",
    )
    _add_scan_arguments(scan_parser, required=True)
    _add_config_arguments(scan_parser, suppress_defaults=True)

    analyze_parser = subparsers.add_parser(
        "analyze",
        help="analyze an existing scan CSV without accessing music files",
    )
    _add_config_arguments(analyze_parser, suppress_defaults=True)
    analyze_parser.add_argument(
        "--scan-report",
        type=Path,
        default=DEFAULT_SCAN_REPORT_PATH,
        help="scan CSV to analyze (default: reports/library_scan.csv)",
    )
    analyze_parser.add_argument(
        "--duration-tolerance",
        type=_non_negative_float,
        default=DEFAULT_DURATION_TOLERANCE,
        help="duplicate duration tolerance in seconds (default: 3)",
    )
    return parser


def _print_scan_warnings(result: ScanResult) -> None:
    """Print non-fatal scan errors after all readable files are processed."""
    for record in result.records:
        if record.status == "error":
            print(
                f"Warning: could not read {record.path}: {record.error}",
                file=sys.stderr,
            )
    for error in result.directory_errors:
        print(f"Warning: could not scan directory: {error}", file=sys.stderr)


def _print_scan_summary(outcome: ScanRunOutcome, source: Path) -> None:
    """Print a concise scan summary."""
    result = outcome.scan_result
    print("Scan failed" if outcome.state == "failed" else "Scan complete")
    print(f"Scan root: {source}")
    print(f"Reports directory: {outcome.directory}")
    print(f"Scan state: {outcome.state}")
    if result is not None:
        summary = result.summary
        print(f"Root Library total: {summary.root_library_total}")
        print(f"Archives: {summary.archive_count}")
        print(f"File errors: {summary.file_error_count}")
        print(f"Directory errors: {summary.directory_error_count}")


def _print_analysis_summary(
    analysis: LibraryAnalysis, scan_report: Path, path_mode: str
) -> None:
    """Print the required v0.2 terminal summary."""
    summary = analysis.summary
    print("Analysis complete")
    print(f"Scan report: {scan_report}")
    print(f"Reports directory: {DEFAULT_REPORT_DIRECTORY}")
    print(f"Path mode: {path_mode}")
    print(f"Root Library total: {summary.root_library_total}")
    print(f"Duplicate candidate groups: {summary.duplicate_candidate_groups}")
    print(f"Duplicate candidate files: {summary.duplicate_candidate_files}")
    print(f"Files with missing metadata: {summary.files_with_missing_metadata}")
    print(f"Corrupt/unreadable files: {summary.corrupt_or_unreadable_files}")
    print(f"Low bitrate files: {summary.low_bitrate_files}")


def _run_scan(source_argument: str, config: AppConfig, path_mode: str) -> int:
    """Run the read-only scanner and persist one versioned artifact set."""
    source = Path(source_argument).expanduser().resolve()

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

    try:
        outcome = create_scan_run(
            source,
            DEFAULT_REPORT_DIRECTORY,
            ignore_patterns=config.ignore,
            path_mode=path_mode,
        )
    except ValueError as error:
        print(f"Error: invalid scan configuration: {error}", file=sys.stderr)
        return 2
    except (OSError, csv.Error) as error:
        print(
            f"Error: could not create scan run: {clean_error(error)}",
            file=sys.stderr,
        )
        return 1

    if outcome.scan_result is not None:
        _print_scan_warnings(outcome.scan_result)
    _print_scan_summary(outcome, source)
    if outcome.state == "failed":
        print(f"Error: scan failed: {outcome.error}", file=sys.stderr)
        return 1
    return 0


def _run_analysis(
    scan_report_argument: Path,
    duration_tolerance: float,
    path_mode: str,
) -> int:
    """Analyze one scan CSV and write local findings reports."""
    scan_report = scan_report_argument.expanduser().resolve()
    if not scan_report.exists():
        print(f"Error: scan report does not exist: {scan_report}", file=sys.stderr)
        return 2
    if not scan_report.is_file():
        print(f"Error: scan report is not a file: {scan_report}", file=sys.stderr)
        return 2

    try:
        records = read_scan_report(scan_report, path_mode=path_mode)
        analysis = analyze_library(
            records,
            duration_tolerance=duration_tolerance,
        )
        write_analysis_reports(analysis, DEFAULT_REPORT_DIRECTORY)
    except (OSError, csv.Error, ValueError) as error:
        print(
            f"Error: could not analyze {scan_report}: {clean_error(error)}",
            file=sys.stderr,
        )
        return 1

    _print_analysis_summary(analysis, scan_report, path_mode)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the command-line application and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as error:
        print(f"Error: could not load configuration: {error}", file=sys.stderr)
        return 2
    path_mode = args.path_mode or config.path_mode

    if args.command == "analyze":
        return _run_analysis(
            args.scan_report,
            duration_tolerance=args.duration_tolerance,
            path_mode=path_mode,
        )
    if args.command == "scan":
        return _run_scan(args.source, config, path_mode)
    if args.source:
        return _run_scan(args.source, config, path_mode)

    parser.error("provide --source for a scan or choose the analyze command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
