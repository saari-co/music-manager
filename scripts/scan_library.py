#!/usr/bin/env python3
"""Create a CSV inventory of a music library without modifying source files.

The scanner only reads supported audio files and archive metadata. It never
renames, moves, copies, deletes, retags, or otherwise writes to source files.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import mutagen
except ImportError:  # Reported cleanly by main().
    mutagen = None


AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".wav"}
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | {".zip"}
REPORT_PATH = Path(__file__).resolve().parent.parent / "reports" / "library_scan.csv"
FIELDNAMES = [
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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
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
    return parser.parse_args(argv)


def clean_error(error: BaseException) -> str:
    """Return an error suitable for one CSV cell and one terminal line."""
    message = " ".join(str(error).split())
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


def folder_depth(path: Path, source: Path) -> int:
    """Count directories between source and the file's immediate parent."""
    return max(len(path.relative_to(source).parts) - 1, 0)


def first_tag(tags: Any, keys: Iterable[str]) -> str:
    """Get the first non-empty value from a Mutagen tag mapping."""
    if tags is None:
        return ""

    for key in keys:
        try:
            value = tags.get(key)
        except (AttributeError, KeyError, TypeError):
            continue

        # Native ID3 frames (used by WAV) expose their values through ``text``.
        value = getattr(value, "text", value)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        # Native MP4 track numbers are represented as (track, total) tuples.
        if isinstance(value, tuple):
            current = value[0] if value else ""
            total = value[1] if len(value) > 1 else ""
            value = f"{current}/{total}" if total else current
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def base_row(path: Path, source: Path, file_type: str) -> Dict[str, Any]:
    return {
        "path": str(path),
        "extension": path.suffix.lower(),
        "file_type": file_type,
        "file_size_bytes": "",
        "folder_depth": folder_depth(path, source),
        "artist": "",
        "title": "",
        "album": "",
        "date_year": "",
        "track_number": "",
        "bitrate_kbps": "",
        "duration_seconds": "",
        "is_loose_track": False,
        "is_archive": file_type == "archive",
        "status": "ok",
        "error": "",
    }


def scan_audio_file(
    path: Path, source: Path, is_loose_track: bool
) -> Dict[str, Any]:
    row = base_row(path, source, "audio")
    row["is_loose_track"] = is_loose_track

    try:
        row["file_size_bytes"] = path.stat().st_size
        audio = mutagen.File(path)  # type: ignore[union-attr]
        if audio is None:
            raise ValueError("Mutagen could not identify the audio format")

        tags = audio.tags
        row["artist"] = first_tag(
            tags, ("artist", "albumartist", "TPE1", "TPE2", "\xa9ART", "aART")
        )
        row["title"] = first_tag(tags, ("title", "TIT2", "\xa9nam"))
        row["album"] = first_tag(tags, ("album", "TALB", "\xa9alb"))
        row["date_year"] = first_tag(
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
        row["track_number"] = first_tag(tags, ("tracknumber", "TRCK", "trkn"))

        info = getattr(audio, "info", None)
        bitrate = getattr(info, "bitrate", None)
        duration = getattr(info, "length", None)
        if isinstance(bitrate, (int, float)):
            row["bitrate_kbps"] = round(bitrate / 1000, 2)
        if isinstance(duration, (int, float)):
            row["duration_seconds"] = round(duration, 3)
    except Exception as error:
        row["status"] = "error"
        row["error"] = clean_error(error)
        print(f"Warning: could not read {path}: {row['error']}", file=sys.stderr)

    return row


def scan_archive(path: Path, source: Path) -> Dict[str, Any]:
    row = base_row(path, source, "archive")
    try:
        row["file_size_bytes"] = path.stat().st_size
    except Exception as error:
        row["status"] = "error"
        row["error"] = clean_error(error)
        print(f"Warning: could not read {path}: {row['error']}", file=sys.stderr)
    return row


def discover_files(source: Path) -> Tuple[List[Path], List[str]]:
    paths: List[Path] = []
    directory_errors: List[str] = []

    def report_walk_error(error: OSError) -> None:
        message = clean_error(error)
        directory_errors.append(message)
        print(f"Warning: could not scan directory: {message}", file=sys.stderr)

    for root, directories, filenames in os.walk(
        source, topdown=True, onerror=report_walk_error, followlinks=False
    ):
        directories.sort(key=str.casefold)
        for filename in sorted(filenames, key=str.casefold):
            path = Path(root) / filename
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                paths.append(path)

    return paths, directory_errors


def build_rows(paths: Sequence[Path], source: Path) -> List[Dict[str, Any]]:
    audio_files_per_folder = Counter(
        path.parent for path in paths if path.suffix.lower() in AUDIO_EXTENSIONS
    )
    rows: List[Dict[str, Any]] = []

    for path in paths:
        if path.suffix.lower() == ".zip":
            rows.append(scan_archive(path, source))
        else:
            rows.append(
                scan_audio_file(
                    path,
                    source,
                    is_loose_track=audio_files_per_folder[path.parent] == 1,
                )
            )
    return rows


def write_report(rows: Sequence[Dict[str, Any]], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(
    source: Path,
    rows: Sequence[Dict[str, Any]],
    directory_error_count: int,
    report_path: Path,
) -> None:
    audio_count = sum(row["file_type"] == "audio" for row in rows)
    archive_count = sum(row["file_type"] == "archive" for row in rows)
    loose_count = sum(bool(row["is_loose_track"]) for row in rows)
    file_error_count = sum(row["status"] == "error" for row in rows)

    print("Scan complete")
    print(f"Source: {source}")
    print(f"Report: {report_path}")
    print(f"Audio files: {audio_count}")
    print(f"Archives: {archive_count}")
    print(f"Loose tracks: {loose_count}")
    print(f"File errors: {file_error_count}")
    print(f"Directory errors: {directory_error_count}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    source = Path(args.source).expanduser().resolve()

    if not source.exists():
        print(f"Error: source does not exist: {source}", file=sys.stderr)
        return 2
    if not source.is_dir():
        print(f"Error: source is not a directory: {source}", file=sys.stderr)
        return 2
    if mutagen is None:
        print(
            "Error: mutagen is required. Install it with: "
            "python -m pip install mutagen",
            file=sys.stderr,
        )
        return 2

    paths, directory_errors = discover_files(source)
    rows = build_rows(paths, source)

    try:
        write_report(rows, REPORT_PATH)
    except OSError as error:
        print(
            f"Error: could not write report {REPORT_PATH}: {clean_error(error)}",
            file=sys.stderr,
        )
        return 1

    print_summary(source, rows, len(directory_errors), REPORT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
