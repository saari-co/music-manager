"""Versioned analysis lifecycle bound to one validated schema 1 scan run."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import UUID, uuid4

from music_manager import __version__
from music_manager.analyzer import DEFAULT_DURATION_TOLERANCE, analyze_library
from music_manager.artifact_schema import (
    ArtifactValidationError,
    LibraryScanRow,
    ScanErrorRow,
    ScanManifest,
    ValidatedArtifactSet,
    validate_artifact_set,
)
from music_manager.models import LibraryAnalysis, ScanRecord
from music_manager.reports import (
    AnalysisReportSpec,
    versioned_analysis_report_specs,
)


Clock = Callable[[], datetime]


@dataclass(frozen=True)
class AnalysisRunOutcome:
    """A successfully registered analysis of one versioned scan run."""

    directory: Path
    manifest: ScanManifest
    analysis: LibraryAnalysis


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("analysis run clock must return timezone-aware values")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_text(value: object | None) -> str:
    return "" if value is None else str(value)


def _error_messages(
    errors: Sequence[ScanErrorRow],
) -> Mapping[UUID, tuple[str, ...]]:
    messages: dict[UUID, list[str]] = defaultdict(list)
    for error in errors:
        if error.file_record_id is None or error.severity not in {"error", "fatal"}:
            continue
        messages[error.file_record_id].append(f"{error.error_code}: {error.message}")
    return {
        file_record_id: tuple(record_messages)
        for file_record_id, record_messages in messages.items()
    }


def _scan_record(
    row: LibraryScanRow,
    messages: Mapping[UUID, tuple[str, ...]],
) -> ScanRecord:
    return ScanRecord(
        path=Path(row.path),
        extension=row.extension,
        file_type=row.file_type,
        file_size_bytes=row.file_size_bytes,
        modified_time_ns=row.modified_time_ns,
        scan_id=row.scan_id,
        file_record_id=row.file_record_id,
        file_fingerprint=row.file_fingerprint,
        relative_path=row.path,
        artist=row.artist,
        album_artist=row.album_artist,
        title=row.title,
        album=row.album,
        date=row.date,
        date_year=_optional_text(row.release_year),
        track_number=_optional_text(row.track_number),
        release_year=row.release_year,
        parsed_track_number=row.track_number,
        track_total=row.track_total,
        disc_number=row.disc_number,
        disc_total=row.disc_total,
        genre=row.genre,
        composer=row.composer,
        is_compilation=row.is_compilation,
        codec=row.codec,
        container=row.container,
        bitrate_kbps=(None if row.bitrate_kbps is None else float(row.bitrate_kbps)),
        duration_seconds=(
            None if row.duration_seconds is None else float(row.duration_seconds)
        ),
        sample_rate_hz=row.sample_rate_hz,
        bit_depth=row.bit_depth,
        channels=row.channels,
        is_archive=row.file_type == "archive",
        status=row.record_status,
        error="; ".join(messages.get(row.file_record_id, ())),
    )


def _analysis_records(artifacts: ValidatedArtifactSet) -> list[ScanRecord]:
    messages = _error_messages(artifacts.error_rows)
    rows = sorted(
        artifacts.library_rows,
        key=lambda row: (row.path.casefold(), row.path),
    )
    return [_scan_record(row, messages) for row in rows]


def _temporary_path(path: Path) -> Path:
    return path.parent / f".{path.name}.{uuid4().hex}.tmp"


def _stage_csv(
    directory: Path,
    spec: AnalysisReportSpec,
) -> tuple[Path, str, int]:
    final_path = directory / spec.filename
    temporary = _temporary_path(final_path)
    row_count = 0
    digest = hashlib.sha256()
    try:
        with temporary.open("x", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=spec.fieldnames,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in spec.rows:
                writer.writerow(row)
                row_count += 1
            output.flush()
            os.fsync(output.fileno())
        with temporary.open("rb") as staged:
            for block in iter(lambda: staged.read(1024 * 1024), b""):
                digest.update(block)
        return temporary, digest.hexdigest(), row_count
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _derived_entry(
    spec: AnalysisReportSpec,
    generated_at: str,
    row_count: int,
    digest: str,
    configuration: Mapping[str, object],
) -> dict[str, object]:
    return {
        "filename": spec.filename,
        "role": "derived",
        "application_version": __version__,
        "generated_at": generated_at,
        "row_count": row_count,
        "sha256": digest,
        "configuration": dict(configuration),
    }


def _manifest_with_artifacts(
    manifest: ScanManifest,
    artifacts: Mapping[str, Mapping[str, object]],
) -> ScanManifest:
    data = manifest.to_dict()
    data["artifacts"] = dict(artifacts)
    return ScanManifest.from_dict(data)


def _primary_manifest(manifest: ScanManifest) -> ScanManifest:
    primary_artifacts = {
        name: entry.to_dict()
        for name, entry in manifest.artifacts.items()
        if entry.role == "primary"
    }
    return _manifest_with_artifacts(manifest, primary_artifacts)


def _atomic_write_manifest(path: Path, manifest: ScanManifest) -> None:
    temporary = _temporary_path(path)
    payload = json.dumps(
        manifest.to_dict(),
        indent=2,
        ensure_ascii=False,
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(f"{payload}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def analyze_scan_run(
    run_directory: Path,
    *,
    duration_tolerance: float = DEFAULT_DURATION_TOLERANCE,
    clock: Clock = _utc_now,
) -> AnalysisRunOutcome:
    """Analyze and atomically register all derived artifacts for one scan."""
    if not math.isfinite(duration_tolerance) or duration_tolerance < 0:
        raise ValueError("duration tolerance must be a finite non-negative value")
    if run_directory.is_symlink():
        raise ArtifactValidationError("scan run directory cannot be a symlink")
    if not run_directory.is_dir():
        raise ArtifactValidationError(
            f"scan run directory does not exist: {run_directory}"
        )

    manifest_path = run_directory / "scan_manifest.json"
    artifacts = validate_artifact_set(manifest_path)
    manifest = artifacts.manifest
    if run_directory.name != str(manifest.scan_id):
        raise ArtifactValidationError(
            "scan run directory name does not match the manifest scan_id"
        )
    if manifest.state not in {"complete", "incomplete"}:
        raise ArtifactValidationError(
            f"scan state {manifest.state!r} cannot be analyzed"
        )

    analysis = analyze_library(
        _analysis_records(artifacts),
        duration_tolerance=duration_tolerance,
    )
    specs = versioned_analysis_report_specs(analysis, manifest.scan_id)
    staged: list[tuple[AnalysisReportSpec, Path | None, str, int]] = []
    try:
        for spec in specs:
            temporary, digest, row_count = _stage_csv(run_directory, spec)
            staged.append((spec, temporary, digest, row_count))

        generated_at = _timestamp(clock())
        configuration: Mapping[str, object] = {
            "duration_tolerance": duration_tolerance,
        }
        derived_entries = {
            spec.logical_name: _derived_entry(
                spec,
                generated_at,
                row_count,
                digest,
                configuration,
            )
            for spec, _temporary, digest, row_count in staged
        }
        primary_manifest = _primary_manifest(manifest)
        if primary_manifest.artifacts != manifest.artifacts:
            _atomic_write_manifest(manifest_path, primary_manifest)

        for index, (spec, temporary, digest, row_count) in enumerate(staged):
            if temporary is None:
                raise AssertionError("analysis report was already finalized")
            os.replace(temporary, run_directory / spec.filename)
            staged[index] = (spec, None, digest, row_count)

        final_artifacts = {
            name: entry.to_dict() for name, entry in primary_manifest.artifacts.items()
        }
        final_artifacts.update(derived_entries)
        final_manifest = _manifest_with_artifacts(
            primary_manifest,
            final_artifacts,
        )
        _atomic_write_manifest(manifest_path, final_manifest)
        return AnalysisRunOutcome(
            directory=run_directory,
            manifest=final_manifest,
            analysis=analysis,
        )
    finally:
        for _spec, temporary, _digest, _row_count in staged:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
