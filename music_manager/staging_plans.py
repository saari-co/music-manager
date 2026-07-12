"""Approval parsing and plan-only staging artifact registration."""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import RFC_4122, UUID, uuid4

from music_manager import __version__
from music_manager.artifact_schema import (
    STAGING_ARTIFACT_NAMES,
    STAGING_PLAN_HEADER,
    ArtifactValidationError,
    LibraryScanRow,
    ScanManifest,
    StagingPlanRow,
    required_schema_version,
    validate_artifact_set,
)


APPROVAL_HEADER = ("scan_id", "file_record_id", "decision")
APPROVAL_DECISIONS = frozenset({"stage", "skip"})
Clock = Callable[[], datetime]
StageIdFactory = Callable[[], UUID]


@dataclass(frozen=True)
class ApprovalRow:
    """One strictly validated user-controlled approval row."""

    scan_id: UUID
    file_record_id: UUID
    decision: str


@dataclass(frozen=True)
class StagingPlanOutcome:
    """One successfully registered staging plan."""

    directory: Path
    manifest: ScanManifest
    stage_id: UUID
    rows: tuple[StagingPlanRow, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("staging plan clock must return timezone-aware values")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_uuid(value: str, location: str, *, version: int) -> UUID:
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError) as error:
        raise ArtifactValidationError(
            f"{location}: must be a UUIDv{version}"
        ) from error
    if parsed.version != version or parsed.variant != RFC_4122 or str(parsed) != value:
        raise ArtifactValidationError(
            f"{location}: must be a lowercase canonical UUIDv{version}"
        )
    return parsed


def load_approval_file(path: Path, scan_id: UUID) -> tuple[ApprovalRow, ...]:
    """Read a strict approval CSV without modifying it."""
    rows: list[ApprovalRow] = []
    seen_file_ids: set[UUID] = set()
    try:
        with path.open("r", encoding="utf-8", newline="") as input_file:
            reader = csv.DictReader(input_file, strict=True)
            if reader.fieldnames is None:
                raise ArtifactValidationError("approval CSV: missing header")
            if tuple(reader.fieldnames) != APPROVAL_HEADER:
                raise ArtifactValidationError(
                    "approval CSV: header must be exactly " + ",".join(APPROVAL_HEADER)
                )
            for row_number, value in enumerate(reader, start=2):
                location = f"approval CSV row {row_number}"
                if None in value or set(value) != set(APPROVAL_HEADER):
                    raise ArtifactValidationError(f"{location}: malformed CSV row")
                if any(value[name] is None for name in APPROVAL_HEADER):
                    raise ArtifactValidationError(f"{location}: malformed CSV row")
                row_scan_id = _parse_uuid(
                    value["scan_id"], f"{location}.scan_id", version=4
                )
                if row_scan_id != scan_id:
                    raise ArtifactValidationError(
                        f"{location}.scan_id: does not match selected scan"
                    )
                file_record_id = _parse_uuid(
                    value["file_record_id"],
                    f"{location}.file_record_id",
                    version=5,
                )
                if file_record_id in seen_file_ids:
                    raise ArtifactValidationError(
                        f"{location}.file_record_id: duplicate approval row"
                    )
                seen_file_ids.add(file_record_id)
                decision = value["decision"]
                if decision not in APPROVAL_DECISIONS:
                    raise ArtifactValidationError(
                        f"{location}.decision: must be 'stage' or 'skip'"
                    )
                rows.append(
                    ApprovalRow(
                        scan_id=row_scan_id,
                        file_record_id=file_record_id,
                        decision=decision,
                    )
                )
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise ArtifactValidationError(f"approval CSV: cannot read: {error}") from error
    return tuple(rows)


def _inventory_by_id(rows: Sequence[LibraryScanRow]) -> dict[UUID, LibraryScanRow]:
    inventory: dict[UUID, LibraryScanRow] = {}
    for row in rows:
        if row.file_record_id in inventory:
            raise ArtifactValidationError(
                "library_scan.csv: file_record_id must identify exactly one row"
            )
        inventory[row.file_record_id] = row
    return inventory


def _plan_rows(
    scan_id: UUID,
    stage_id: UUID,
    approvals: Sequence[ApprovalRow],
    inventory: Mapping[UUID, LibraryScanRow],
) -> tuple[StagingPlanRow, ...]:
    selected = [row for row in approvals if row.decision == "stage"]
    if not selected:
        raise ArtifactValidationError(
            "approval CSV: must select at least one row for staging"
        )

    planned: list[StagingPlanRow] = []
    for approval in selected:
        scan_row = inventory.get(approval.file_record_id)
        if scan_row is None:
            raise ArtifactValidationError(
                "approval CSV: file_record_id does not identify an inventory row"
            )
        if scan_row.file_type != "audio":
            status, reason = "not_eligible", "not_audio"
        elif scan_row.record_status != "ok":
            status, reason = "not_eligible", "record_not_ok"
        else:
            status, reason = "planned", ""
        row = StagingPlanRow(
            scan_id=scan_id,
            stage_id=stage_id,
            file_record_id=scan_row.file_record_id,
            source_path=scan_row.path,
            stage_relative_path=f"files/{scan_row.path}",
            plan_status=status,
            reason_code=reason,
        )
        StagingPlanRow.from_csv_row(row.to_csv_row())
        planned.append(row)
    return tuple(sorted(planned, key=lambda row: row.source_path))


def _temporary_path(path: Path) -> Path:
    return path.parent / f".{path.name}.{uuid4().hex}.tmp"


def _stage_plan_csv(
    run_directory: Path, rows: Sequence[StagingPlanRow]
) -> tuple[Path, str]:
    final_path = run_directory / "staging_plan.csv"
    temporary = _temporary_path(final_path)
    digest = hashlib.sha256()
    try:
        with temporary.open("x", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=STAGING_PLAN_HEADER)
            writer.writeheader()
            writer.writerows(row.to_csv_row() for row in rows)
            output.flush()
            os.fsync(output.fileno())
        with temporary.open("rb") as input_file:
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                digest.update(chunk)
        return temporary, digest.hexdigest()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _manifest_without_staging(manifest: ScanManifest) -> ScanManifest:
    data = manifest.to_dict()
    data["artifacts"] = {
        name: entry.to_dict()
        for name, entry in manifest.artifacts.items()
        if name not in STAGING_ARTIFACT_NAMES
    }
    return ScanManifest.from_dict(data)


def _manifest_with_plan(
    base_manifest: ScanManifest,
    generated_at: str,
    row_count: int,
    digest: str,
) -> ScanManifest:
    data = base_manifest.to_dict()
    artifacts = dict(data["artifacts"])
    artifacts["staging_plan"] = {
        "filename": "staging_plan.csv",
        "role": "derived",
        "application_version": __version__,
        "generated_at": generated_at,
        "row_count": row_count,
        "sha256": digest,
        "configuration": {},
    }
    data["artifacts"] = artifacts
    data["schema_version"] = required_schema_version(artifacts)
    return ScanManifest.from_dict(data)


def _atomic_write_manifest(path: Path, manifest: ScanManifest) -> None:
    temporary = _temporary_path(path)
    payload = json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False)
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(f"{payload}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_staging_plan(
    run_directory: Path,
    approval_path: Path,
    *,
    stage_id_factory: StageIdFactory = uuid4,
    clock: Clock = _utc_now,
) -> StagingPlanOutcome:
    """Create and atomically register reviewable plan evidence only."""
    if run_directory.is_symlink():
        raise ArtifactValidationError("scan run directory cannot be a symlink")
    if not run_directory.is_dir():
        raise ArtifactValidationError(
            f"scan run directory does not exist: {run_directory}"
        )

    manifest_path = run_directory / "scan_manifest.json"
    artifacts = validate_artifact_set(manifest_path)
    original_manifest = artifacts.manifest
    if run_directory.name != str(original_manifest.scan_id):
        raise ArtifactValidationError(
            "scan run directory name does not match the manifest scan_id"
        )
    if original_manifest.state not in {"complete", "incomplete"}:
        raise ArtifactValidationError(
            f"scan state {original_manifest.state!r} cannot register a staging plan"
        )

    approvals = load_approval_file(approval_path, original_manifest.scan_id)
    inventory = _inventory_by_id(artifacts.library_rows)
    unknown = [row for row in approvals if row.file_record_id not in inventory]
    if unknown:
        raise ArtifactValidationError(
            "approval CSV: file_record_id does not identify an inventory row"
        )

    stage_id = stage_id_factory()
    if stage_id.version != 4 or stage_id.variant != RFC_4122:
        raise ValueError("stage_id factory must return UUIDv4 values")
    rows = _plan_rows(original_manifest.scan_id, stage_id, approvals, inventory)
    generated_at = _timestamp(clock())

    staged_path: Path | None = None
    backup_path: Path | None = None
    final_path = run_directory / "staging_plan.csv"
    manifest_changed = False
    final_replaced = False
    conflict_detected = False
    try:
        staged_path, digest = _stage_plan_csv(run_directory, rows)
        base_manifest = _manifest_without_staging(original_manifest)
        final_manifest = _manifest_with_plan(
            base_manifest, generated_at, len(rows), digest
        )

        # Phase 1: durably remove the stale staging_plan entry (if any) before
        # touching staging_plan.csv. A crash after this point and before phase
        # 2 below leaves a self-consistent manifest with no staging_plan entry.
        if base_manifest.artifacts != original_manifest.artifacts:
            _atomic_write_manifest(manifest_path, base_manifest)
            manifest_changed = True

        pre_swap_snapshot = manifest_path.read_bytes()

        if final_path.is_symlink() or final_path.exists():
            candidate_backup = _temporary_path(final_path)
            os.replace(final_path, candidate_backup)
            backup_path = candidate_backup
        os.replace(staged_path, final_path)
        staged_path = None
        final_replaced = True

        # Best-effort CAS: another registration (analysis, matching, or a
        # concurrent staging run) may have replaced the manifest while we were
        # swapping staging_plan.csv into place. Detect it before phase 2 so we
        # never overwrite that interloper's manifest with stale content.
        if manifest_path.read_bytes() != pre_swap_snapshot:
            conflict_detected = True
            raise ArtifactValidationError(
                "scan manifest changed during staging plan registration"
            )

        # Phase 2: register the new entry now that its digest matches the
        # file actually on disk.
        _atomic_write_manifest(manifest_path, final_manifest)
        manifest_changed = True
        validated = validate_artifact_set(manifest_path)
        if backup_path is not None:
            backup_path.unlink(missing_ok=True)
        return StagingPlanOutcome(
            directory=run_directory,
            manifest=validated.manifest,
            stage_id=stage_id,
            rows=validated.staging_plan_rows,
        )
    except Exception as error:
        rollback_errors: list[Exception] = []
        if final_replaced:
            try:
                final_path.unlink(missing_ok=True)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        # The backup is unlinked only after a confirmed successful restore (or
        # below, when there was never a backup to begin with). If the restore
        # itself fails, the backup file must survive on disk untouched so the
        # prior valid staging_plan.csv is never permanently lost.
        csv_restored = backup_path is None
        if backup_path is not None:
            try:
                os.replace(backup_path, final_path)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
            else:
                csv_restored = True
                backup_path = None
        # Restoring the original manifest (which references the old
        # staging_plan digest) is only safe once staging_plan.csv is confirmed
        # back to its original bytes, and only when nobody else has since
        # written a newer manifest we would otherwise clobber.
        if manifest_changed and csv_restored and not conflict_detected:
            try:
                _atomic_write_manifest(manifest_path, original_manifest)
            except Exception as rollback_error:
                rollback_errors.append(rollback_error)
        if rollback_errors:
            error.add_note(
                "staging plan rollback also failed: "
                + "; ".join(str(value) for value in rollback_errors)
            )
        raise
    finally:
        if staged_path is not None:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError:
                pass
