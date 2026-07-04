"""Durable primary artifact lifecycle for one read-only library scan."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence
from uuid import RFC_4122, UUID, uuid4

from music_manager import __version__
from music_manager.artifact_schema import (
    LIBRARY_SCAN_HEADER,
    SCHEMA_VERSION,
    SCAN_ERRORS_HEADER,
    ScanConfiguration,
    ScanErrorRow,
    ScanManifest,
)
from music_manager.models import ScanFinding, ScanResult
from music_manager.scanner import MetadataLoader, scan_library
from music_manager.utils import clean_error


Clock = Callable[[], datetime]
ScanIdFactory = Callable[[], UUID]


@dataclass(frozen=True)
class ScanRunOutcome:
    """Final in-memory result of a scan run lifecycle."""

    directory: Path
    manifest: ScanManifest
    scan_result: Optional[ScanResult]
    error: str = ""

    @property
    def state(self) -> str:
        """Return the final manifest state."""
        return self.manifest.state


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scan run clock must return timezone-aware values")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_scan_id(scan_id: UUID) -> None:
    if scan_id.version != 4 or scan_id.variant != RFC_4122:
        raise ValueError("scan_id factory must return UUIDv4 values")


def _create_run_directory(
    reports_root: Path,
    scan_id_factory: ScanIdFactory,
) -> tuple[UUID, Path]:
    if reports_root.is_symlink():
        raise ValueError("reports directory cannot be a symlink")
    reports_root.mkdir(parents=True, exist_ok=True)
    for _attempt in range(100):
        scan_id = scan_id_factory()
        _validate_scan_id(scan_id)
        directory = reports_root / str(scan_id)
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        return scan_id, directory
    raise FileExistsError("could not allocate a unique scan run directory")


def _manifest_data(
    *,
    scan_id: UUID,
    state: str,
    started_at: str,
    completed_at: Optional[str],
    artifacts: Mapping[str, Mapping[str, object]],
    inventory_rows: int,
    info_findings: int,
    error_findings: int,
    fatal_findings: int,
    skipped_symlinks: int,
    configuration: ScanConfiguration,
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "application_version": __version__,
        "scan_id": str(scan_id),
        "state": state,
        "started_at": started_at,
        "completed_at": completed_at,
        "artifacts": dict(artifacts),
        "counts": {
            "inventory_rows": inventory_rows,
            "info_findings": info_findings,
            "error_findings": error_findings,
            "fatal_findings": fatal_findings,
            "skipped_symlinks": skipped_symlinks,
        },
        "configuration": configuration.to_dict(),
    }


def _build_manifest(
    *,
    scan_id: UUID,
    state: str,
    started_at: str,
    completed_at: Optional[str],
    artifacts: Mapping[str, Mapping[str, object]],
    inventory_rows: int,
    info_findings: int,
    error_findings: int,
    fatal_findings: int,
    skipped_symlinks: int,
    configuration: ScanConfiguration,
) -> ScanManifest:
    return ScanManifest.from_dict(
        _manifest_data(
            scan_id=scan_id,
            state=state,
            started_at=started_at,
            completed_at=completed_at,
            artifacts=artifacts,
            inventory_rows=inventory_rows,
            info_findings=info_findings,
            error_findings=error_findings,
            fatal_findings=fatal_findings,
            skipped_symlinks=skipped_symlinks,
            configuration=configuration,
        )
    )


def _temporary_path(path: Path) -> Path:
    return path.parent / f".{path.name}.{uuid4().hex}.tmp"


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


def _stage_csv(
    directory: Path,
    filename: str,
    header: Sequence[str],
    rows: Iterable[Mapping[str, str]],
) -> tuple[Path, str, int]:
    final_path = directory / filename
    temporary = _temporary_path(final_path)
    row_count = 0
    digest = hashlib.sha256()
    try:
        with temporary.open(
            "x",
            encoding="utf-8",
            newline="",
        ) as output:
            writer = csv.DictWriter(
                output,
                fieldnames=header,
                extrasaction="raise",
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
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


def _artifact_entry(
    filename: str,
    generated_at: str,
    row_count: int,
    digest: str,
) -> dict[str, object]:
    return {
        "filename": filename,
        "role": "primary",
        "application_version": __version__,
        "generated_at": generated_at,
        "row_count": row_count,
        "sha256": digest,
    }


def _sanitize_text(text: str, private_roots: Sequence[Path]) -> str:
    sanitized = text
    for root in private_roots:
        root_text = str(root)
        if not root_text:
            continue
        sanitized = sanitized.replace(f"{root_text}{os.sep}", "")
        sanitized = sanitized.replace(root_text, ".")
    return sanitized


def _finding_rows(
    scan_result: ScanResult,
    private_roots: Sequence[Path],
) -> tuple[ScanErrorRow, ...]:
    rows: list[ScanErrorRow] = []
    for finding in scan_result.findings:
        sanitized = ScanFinding(
            path=finding.path,
            stage=finding.stage,
            severity=finding.severity,
            error_code=finding.error_code,
            message=_sanitize_text(finding.message, private_roots),
            file_record_id=finding.file_record_id,
        )
        rows.append(sanitized.to_scan_error_row(scan_result.scan_id))
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.path.casefold(),
                row.path,
                row.stage,
                row.error_code,
                row.severity,
            ),
        )
    )


def _replace_staged(
    staged: Sequence[tuple[Path, Path]],
) -> None:
    for temporary, final_path in staged:
        os.replace(temporary, final_path)


def _cleanup_primary_artifacts(directory: Path) -> None:
    for filename in ("library_scan.csv", "scan_errors.csv"):
        (directory / filename).unlink(missing_ok=True)
    for temporary in directory.glob(".*.tmp"):
        temporary.unlink(missing_ok=True)


def _finalize_success(
    directory: Path,
    started_at: str,
    configuration: ScanConfiguration,
    scan_result: ScanResult,
    clock: Clock,
) -> ScanRunOutcome:
    library_rows = tuple(
        sorted(
            scan_result.to_library_scan_rows(),
            key=lambda row: (row.path.casefold(), row.path),
        )
    )
    error_rows = _finding_rows(
        scan_result,
        (scan_result.source, directory.parent, Path.home(), Path.cwd()),
    )
    if any(row.severity == "fatal" for row in error_rows):
        raise RuntimeError("fatal scan finding prevented a usable inventory")
    library_temp: Optional[Path] = None
    errors_temp: Optional[Path] = None
    try:
        library_temp, library_digest, library_count = _stage_csv(
            directory,
            "library_scan.csv",
            LIBRARY_SCAN_HEADER,
            (row.to_csv_row() for row in library_rows),
        )
        errors_temp, errors_digest, errors_count = _stage_csv(
            directory,
            "scan_errors.csv",
            SCAN_ERRORS_HEADER,
            (row.to_csv_row() for row in error_rows),
        )
        generated_at = _timestamp(clock())
        _replace_staged(
            (
                (library_temp, directory / "library_scan.csv"),
                (errors_temp, directory / "scan_errors.csv"),
            )
        )
        library_temp = None
        errors_temp = None

        severity_counts = {
            severity: sum(row.severity == severity for row in error_rows)
            for severity in ("info", "error", "fatal")
        }
        state = "incomplete" if severity_counts["error"] else "complete"
        artifacts = {
            "library_scan": _artifact_entry(
                "library_scan.csv",
                generated_at,
                library_count,
                library_digest,
            ),
            "scan_errors": _artifact_entry(
                "scan_errors.csv",
                generated_at,
                errors_count,
                errors_digest,
            ),
        }
        manifest = _build_manifest(
            scan_id=scan_result.scan_id,
            state=state,
            started_at=started_at,
            completed_at=generated_at,
            artifacts=artifacts,
            inventory_rows=library_count,
            info_findings=severity_counts["info"],
            error_findings=severity_counts["error"],
            fatal_findings=severity_counts["fatal"],
            skipped_symlinks=sum(
                row.error_code == "symlink_skipped" for row in error_rows
            ),
            configuration=configuration,
        )
        _atomic_write_manifest(directory / "scan_manifest.json", manifest)
        return ScanRunOutcome(
            directory=directory,
            manifest=manifest,
            scan_result=scan_result,
        )
    finally:
        if library_temp is not None:
            library_temp.unlink(missing_ok=True)
        if errors_temp is not None:
            errors_temp.unlink(missing_ok=True)


def _fatal_row(
    scan_id: UUID,
    error: BaseException,
    private_roots: Sequence[Path],
) -> ScanErrorRow:
    message = _sanitize_text(clean_error(error), private_roots)
    return ScanErrorRow.from_csv_row(
        {
            "scan_id": str(scan_id),
            "file_record_id": "",
            "path": "",
            "stage": "finalization",
            "severity": "fatal",
            "error_code": "scan_failed",
            "message": message,
        },
        location="fatal scan finding",
    )


def _finalize_failed(
    directory: Path,
    source: Path,
    scan_id: UUID,
    started_at: str,
    configuration: ScanConfiguration,
    error: Exception,
    clock: Clock,
    scan_result: Optional[ScanResult],
) -> ScanRunOutcome:
    _cleanup_primary_artifacts(directory)
    private_roots = (
        source,
        directory.parent,
        Path.home(),
        Path.cwd(),
    )
    existing_rows = (
        _finding_rows(scan_result, private_roots) if scan_result is not None else ()
    )
    error_rows = (*existing_rows, _fatal_row(scan_id, error, private_roots))
    artifacts: dict[str, Mapping[str, object]] = {}
    try:
        temporary, digest, row_count = _stage_csv(
            directory,
            "scan_errors.csv",
            SCAN_ERRORS_HEADER,
            (row.to_csv_row() for row in error_rows),
        )
        os.replace(temporary, directory / "scan_errors.csv")
        generated_at = _timestamp(clock())
        artifacts["scan_errors"] = _artifact_entry(
            "scan_errors.csv",
            generated_at,
            row_count,
            digest,
        )
    except Exception:
        _cleanup_primary_artifacts(directory)
        generated_at = _timestamp(clock())

    severity_counts = {
        severity: sum(row.severity == severity for row in error_rows)
        for severity in ("info", "error", "fatal")
    }
    manifest = _build_manifest(
        scan_id=scan_id,
        state="failed",
        started_at=started_at,
        completed_at=generated_at,
        artifacts=artifacts,
        inventory_rows=0,
        info_findings=severity_counts["info"],
        error_findings=severity_counts["error"],
        fatal_findings=severity_counts["fatal"],
        skipped_symlinks=sum(row.error_code == "symlink_skipped" for row in error_rows),
        configuration=configuration,
    )
    try:
        _atomic_write_manifest(directory / "scan_manifest.json", manifest)
    except OSError:
        pass
    return ScanRunOutcome(
        directory=directory,
        manifest=manifest,
        scan_result=scan_result,
        error=error_rows[-1].message,
    )


def create_scan_run(
    source: Path,
    reports_root: Path,
    *,
    ignore_patterns: Sequence[str] = (),
    path_mode: str = "relative",
    metadata_loader: Optional[MetadataLoader] = None,
    scan_id_factory: ScanIdFactory = uuid4,
    clock: Clock = _utc_now,
) -> ScanRunOutcome:
    """Create one exclusive primary artifact set for a read-only scan."""
    configuration = ScanConfiguration.from_dict(
        {
            "ignore": list(ignore_patterns),
            "path_mode": path_mode,
            "follow_symlinks": False,
        },
        location="scan configuration",
    )
    scan_id, directory = _create_run_directory(
        reports_root,
        scan_id_factory,
    )
    started_at = _timestamp(clock())
    running_manifest = _build_manifest(
        scan_id=scan_id,
        state="running",
        started_at=started_at,
        completed_at=None,
        artifacts={},
        inventory_rows=0,
        info_findings=0,
        error_findings=0,
        fatal_findings=0,
        skipped_symlinks=0,
        configuration=configuration,
    )
    _atomic_write_manifest(
        directory / "scan_manifest.json",
        running_manifest,
    )

    scan_result: Optional[ScanResult] = None
    try:
        scan_result = scan_library(
            source,
            metadata_loader=metadata_loader,
            ignore_patterns=ignore_patterns,
            scan_id=scan_id,
        )
        return _finalize_success(
            directory,
            started_at,
            configuration,
            scan_result,
            clock,
        )
    except Exception as error:
        return _finalize_failed(
            directory,
            source,
            scan_id,
            started_at,
            configuration,
            error,
            clock,
            scan_result,
        )
