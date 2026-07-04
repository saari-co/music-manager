"""Schema 1 models and strict validators for durable report artifacts.

This module validates report files only. Paths stored inside report rows remain
opaque strings and are never opened, resolved, or statted.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TypeVar
from uuid import RFC_4122, UUID, uuid5


SCHEMA_VERSION = "1.0.0"
SUPPORTED_SCHEMA_MAJOR = 1
SUPPORTED_SCHEMA_MINOR = 0

LIBRARY_SCAN_HEADER = (
    "scan_id",
    "file_record_id",
    "file_fingerprint",
    "path",
    "extension",
    "file_type",
    "file_size_bytes",
    "modified_time_ns",
    "artist",
    "album_artist",
    "title",
    "album",
    "date",
    "release_year",
    "track_number",
    "track_total",
    "disc_number",
    "disc_total",
    "genre",
    "composer",
    "is_compilation",
    "codec",
    "container",
    "bitrate_kbps",
    "duration_seconds",
    "sample_rate_hz",
    "bit_depth",
    "channels",
    "record_status",
)
SCAN_ERRORS_HEADER = (
    "scan_id",
    "file_record_id",
    "path",
    "stage",
    "severity",
    "error_code",
    "message",
)

MANIFEST_STATES = frozenset({"running", "complete", "incomplete", "failed"})
ARTIFACT_ROLES = frozenset({"primary", "derived"})
FILE_TYPES = frozenset({"audio", "archive"})
RECORD_STATUSES = frozenset({"ok", "error"})
ERROR_STAGES = frozenset({"discovery", "stat", "metadata", "archive", "finalization"})
ERROR_SEVERITIES = frozenset({"info", "error", "fatal"})

_ARTIFACT_SPECS = {
    "library_scan": ("library_scan.csv", "primary"),
    "scan_errors": ("scan_errors.csv", "primary"),
    "library_analysis": ("library_analysis.csv", "derived"),
    "duplicate_candidates": ("duplicate_candidates.csv", "derived"),
    "missing_metadata": ("missing_metadata.csv", "derived"),
    "corrupt_files": ("corrupt_files.csv", "derived"),
    "quality_summary": ("quality_summary.csv", "derived"),
}
_MANIFEST_FIELDS = (
    "schema_version",
    "application_version",
    "scan_id",
    "state",
    "started_at",
    "completed_at",
    "artifacts",
    "counts",
    "configuration",
)
_COUNT_FIELDS = (
    "inventory_rows",
    "info_findings",
    "error_findings",
    "fatal_findings",
    "skipped_symlinks",
)
_CONFIG_FIELDS = ("ignore", "path_mode", "follow_symlinks")
_ARTIFACT_FIELDS = (
    "filename",
    "role",
    "application_version",
    "generated_at",
    "row_count",
    "sha256",
)

_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_RFC3339_UTC_RE = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2})(?:\.([0-9]+))?Z$"
)
_SIGNED_INTEGER_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)$")
_UNSIGNED_INTEGER_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")
_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT_RE = re.compile(r"^stat-v1:[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_EXTENSION_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]*$")

_Row = TypeVar("_Row")


class ArtifactValidationError(ValueError):
    """Raised when an artifact violates the schema 1 contract."""


class UnsupportedSchemaVersionError(ArtifactValidationError):
    """Raised when a manifest uses a schema version this reader cannot load."""


def _error(location: str, message: str) -> ArtifactValidationError:
    return ArtifactValidationError(f"{location}: {message}")


def _expect_mapping(value: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise _error(location, "must be a JSON object")
    return value


def _validate_keys(
    value: Mapping[str, Any],
    required: Sequence[str],
    location: str,
    *,
    optional: Sequence[str] = (),
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = [name for name in required if name not in value]
    unexpected = sorted((name for name in value if name not in allowed), key=str)
    if missing:
        raise _error(location, f"missing fields: {', '.join(missing)}")
    if unexpected:
        raise _error(location, f"unexpected fields: {', '.join(unexpected)}")


def _expect_text(
    value: Any,
    location: str,
    *,
    allow_empty: bool = False,
    require_trimmed: bool = True,
) -> str:
    if not isinstance(value, str):
        raise _error(location, "must be a string")
    if not allow_empty and value == "":
        raise _error(location, "must not be empty")
    if require_trimmed and value != value.strip():
        raise _error(location, "must not contain surrounding whitespace")
    return value


def _expect_nonnegative_json_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(location, "must be a non-negative integer")
    if value < 0:
        raise _error(location, "must be a non-negative integer")
    return value


def _parse_schema_version(value: Any, location: str) -> str:
    version = _expect_text(value, location)
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        raise _error(location, "must use major.minor.patch numeric form")
    major, minor, _patch = (int(part) for part in match.groups())
    if major != SUPPORTED_SCHEMA_MAJOR:
        raise UnsupportedSchemaVersionError(
            f"{location}: unsupported schema major {major}; "
            f"supported major is {SUPPORTED_SCHEMA_MAJOR}"
        )
    if minor > SUPPORTED_SCHEMA_MINOR:
        raise UnsupportedSchemaVersionError(
            f"{location}: unsupported schema minor {minor}; "
            f"maximum supported minor is {SUPPORTED_SCHEMA_MINOR}"
        )
    return version


def _parse_uuid(value: Any, location: str, *, version: int) -> UUID:
    text = _expect_text(value, location)
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError) as error:
        raise _error(location, f"must be a canonical UUIDv{version}") from error
    if str(parsed) != text or parsed.version != version or parsed.variant != RFC_4122:
        raise _error(location, f"must be a canonical lowercase UUIDv{version}")
    return parsed


def _parse_rfc3339_utc(value: Any, location: str) -> str:
    text = _expect_text(value, location)
    match = _RFC3339_UTC_RE.fullmatch(text)
    if match is None:
        raise _error(location, "must be a UTC RFC 3339 timestamp ending in Z")
    try:
        datetime.fromisoformat(f"{match.group(1)}+00:00")
    except ValueError as error:
        raise _error(location, "contains an invalid date or time") from error
    return text


def _rfc3339_order_key(value: str) -> tuple[datetime, Decimal]:
    match = _RFC3339_UTC_RE.fullmatch(value)
    if match is None:
        raise AssertionError("timestamp must be validated before comparison")
    instant = datetime.fromisoformat(f"{match.group(1)}+00:00")
    fraction = Decimal(f"0.{match.group(2) or '0'}")
    return instant, fraction


def _parse_sha256(value: Any, location: str) -> str:
    text = _expect_text(value, location)
    if _SHA256_RE.fullmatch(text) is None:
        raise _error(location, "must be 64 lowercase hexadecimal digits")
    return text


def _validate_relative_path(value: str, location: str) -> str:
    if value == "":
        raise _error(location, "must not be empty")
    if value.startswith("/") or _WINDOWS_DRIVE_RE.match(value):
        raise _error(location, "must be source-relative")
    if "\\" in value:
        raise _error(location, "must use / separators")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _error(location, "must not contain empty, . or .. segments")
    return value


def _validate_artifact_filename(value: Any, location: str) -> str:
    filename = _expect_text(value, location)
    _validate_relative_path(filename, location)
    if "/" in filename:
        raise _error(location, "must name a file in the scan directory")
    return filename


def _validate_ignore_pattern(value: Any, location: str) -> str:
    pattern = _expect_text(value, location)
    if pattern.startswith("/") or _WINDOWS_DRIVE_RE.match(pattern):
        raise _error(location, "must be source-relative")
    if "\\" in pattern:
        raise _error(location, "must use / separators")
    if any(part in {".", ".."} for part in pattern.split("/")):
        raise _error(location, "must not contain . or .. segments")
    return pattern


def _validate_json_value(value: Any, location: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _error(location, "must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{location}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _error(location, "must use string object keys")
            _validate_json_value(item, f"{location}.{key}")
        return
    raise _error(location, "must contain only JSON-native values")


def _parse_csv_integer(
    value: str,
    location: str,
    *,
    nullable: bool,
    nonnegative: bool,
) -> int | None:
    if value == "" and nullable:
        return None
    expression = _UNSIGNED_INTEGER_RE if nonnegative else _SIGNED_INTEGER_RE
    if expression.fullmatch(value) is None:
        qualifier = "non-negative " if nonnegative else ""
        raise _error(location, f"must be a canonical {qualifier}base-10 integer")
    return int(value)


def _parse_csv_decimal(
    value: str,
    location: str,
    *,
    nullable: bool,
) -> Decimal | None:
    if value == "" and nullable:
        return None
    if _DECIMAL_RE.fullmatch(value) is None:
        raise _error(
            location,
            "must be a canonical non-negative decimal without an exponent",
        )
    try:
        return Decimal(value)
    except InvalidOperation as error:  # Defensive; the regex is authoritative.
        raise _error(location, "must be a valid decimal") from error


def _parse_csv_bool(
    value: str,
    location: str,
    *,
    nullable: bool,
) -> bool | None:
    if value == "" and nullable:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise _error(location, "must be true, false, or empty when nullable")


def _parse_csv_text(
    value: str,
    location: str,
    *,
    nullable: bool,
) -> str:
    if not isinstance(value, str):
        raise _error(location, "must be a string")
    if value == "":
        if nullable:
            return value
        raise _error(location, "must not be empty")
    if value != value.strip():
        raise _error(location, "must not contain surrounding whitespace")
    return value


def _validate_csv_row_mapping(
    value: Mapping[str, str],
    expected: Sequence[str],
    location: str,
) -> None:
    if not isinstance(value, Mapping):
        raise _error(location, "must be a mapping")
    missing = [name for name in expected if name not in value]
    extra = [name for name in value if name not in expected]
    if missing:
        raise _error(location, f"missing fields: {', '.join(missing)}")
    if extra:
        raise _error(location, f"unexpected fields: {', '.join(extra)}")
    for name in expected:
        if not isinstance(value[name], str):
            raise _error(f"{location}.{name}", "must be a string")


def make_file_record_id(scan_id: UUID, path: str) -> UUID:
    """Return the schema 1 scan-local identity for one inventory path."""
    if scan_id.version != 4 or scan_id.variant != RFC_4122:
        raise ArtifactValidationError("scan_id: must be a UUIDv4")
    _validate_relative_path(path, "path")
    return uuid5(scan_id, f"file-record-v1\0{path}")


def make_file_fingerprint(file_size_bytes: int, modified_time_ns: int) -> str:
    """Return the schema 1 metadata-only change fingerprint."""
    if (
        isinstance(file_size_bytes, bool)
        or not isinstance(file_size_bytes, int)
        or file_size_bytes < 0
    ):
        raise ArtifactValidationError("file_size_bytes: must be a non-negative integer")
    if isinstance(modified_time_ns, bool) or not isinstance(modified_time_ns, int):
        raise ArtifactValidationError("modified_time_ns: must be an integer")
    payload = f"size={file_size_bytes}\nmtime_ns={modified_time_ns}\n".encode("utf-8")
    return f"stat-v1:{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class ArtifactEntry:
    """One registered primary or derived artifact."""

    filename: str
    role: str
    application_version: str
    generated_at: str
    row_count: int
    sha256: str
    configuration: Mapping[str, Any] | None = None

    @classmethod
    def from_dict(
        cls,
        logical_name: str,
        value: Any,
        *,
        location: str,
    ) -> "ArtifactEntry":
        data = _expect_mapping(value, location)
        expected_spec = _ARTIFACT_SPECS.get(logical_name)
        if expected_spec is None:
            raise _error(location, f"unknown artifact name {logical_name!r}")
        expected_filename, expected_role = expected_spec
        optional = ("configuration",) if expected_role == "derived" else ()
        _validate_keys(data, _ARTIFACT_FIELDS, location, optional=optional)

        filename = _validate_artifact_filename(data["filename"], f"{location}.filename")
        if filename != expected_filename:
            raise _error(
                f"{location}.filename",
                f"must be {expected_filename!r} for {logical_name}",
            )
        role = _expect_text(data["role"], f"{location}.role")
        if role not in ARTIFACT_ROLES or role != expected_role:
            raise _error(
                f"{location}.role",
                f"must be {expected_role!r} for {logical_name}",
            )
        application_version = _expect_text(
            data["application_version"],
            f"{location}.application_version",
        )
        generated_at = _parse_rfc3339_utc(
            data["generated_at"], f"{location}.generated_at"
        )
        row_count = _expect_nonnegative_json_int(
            data["row_count"], f"{location}.row_count"
        )
        sha256 = _parse_sha256(data["sha256"], f"{location}.sha256")

        configuration = None
        if expected_role == "derived":
            if "configuration" not in data:
                raise _error(location, "missing fields: configuration")
            config_value = _expect_mapping(
                data["configuration"], f"{location}.configuration"
            )
            _validate_json_value(config_value, f"{location}.configuration")
            configuration = dict(config_value)

        return cls(
            filename=filename,
            role=role,
            application_version=application_version,
            generated_at=generated_at,
            row_count=row_count,
            sha256=sha256,
            configuration=configuration,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible representation."""
        data: dict[str, Any] = {
            "filename": self.filename,
            "role": self.role,
            "application_version": self.application_version,
            "generated_at": self.generated_at,
            "row_count": self.row_count,
            "sha256": self.sha256,
        }
        if self.role == "derived":
            data["configuration"] = dict(self.configuration or {})
        return data


@dataclass(frozen=True)
class ScanCounts:
    """Manifest summary counts for primary scan artifacts."""

    inventory_rows: int
    info_findings: int
    error_findings: int
    fatal_findings: int
    skipped_symlinks: int

    @classmethod
    def from_dict(cls, value: Any, *, location: str) -> "ScanCounts":
        data = _expect_mapping(value, location)
        _validate_keys(data, _COUNT_FIELDS, location)
        parsed = {
            name: _expect_nonnegative_json_int(data[name], f"{location}.{name}")
            for name in _COUNT_FIELDS
        }
        if parsed["skipped_symlinks"] > parsed["info_findings"]:
            raise _error(
                f"{location}.skipped_symlinks",
                "must not exceed info_findings",
            )
        return cls(**parsed)

    @property
    def finding_rows(self) -> int:
        """Return the total number of structured finding rows."""
        return self.info_findings + self.error_findings + self.fatal_findings

    def to_dict(self) -> dict[str, int]:
        """Return the canonical JSON-compatible representation."""
        return {
            "inventory_rows": self.inventory_rows,
            "info_findings": self.info_findings,
            "error_findings": self.error_findings,
            "fatal_findings": self.fatal_findings,
            "skipped_symlinks": self.skipped_symlinks,
        }


@dataclass(frozen=True)
class ScanConfiguration:
    """Sanitized effective scan configuration persisted in a manifest."""

    ignore: tuple[str, ...]
    path_mode: str
    follow_symlinks: bool

    @classmethod
    def from_dict(cls, value: Any, *, location: str) -> "ScanConfiguration":
        data = _expect_mapping(value, location)
        _validate_keys(data, _CONFIG_FIELDS, location)
        ignore_value = data["ignore"]
        if not isinstance(ignore_value, list):
            raise _error(f"{location}.ignore", "must be an array")
        ignore = tuple(
            _validate_ignore_pattern(item, f"{location}.ignore[{index}]")
            for index, item in enumerate(ignore_value)
        )

        path_mode = _expect_text(data["path_mode"], f"{location}.path_mode")
        if path_mode == "absolute":
            raise _error(
                f"{location}.path_mode",
                "schema 1 rejects absolute path output",
            )
        if path_mode != "relative":
            raise _error(f"{location}.path_mode", "must be 'relative'")

        follow_symlinks = data["follow_symlinks"]
        if not isinstance(follow_symlinks, bool):
            raise _error(f"{location}.follow_symlinks", "must be a boolean")
        if follow_symlinks:
            raise _error(
                f"{location}.follow_symlinks",
                "schema 1 does not permit following symlinks",
            )
        return cls(
            ignore=ignore,
            path_mode=path_mode,
            follow_symlinks=follow_symlinks,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible representation."""
        return {
            "ignore": list(self.ignore),
            "path_mode": self.path_mode,
            "follow_symlinks": self.follow_symlinks,
        }


@dataclass(frozen=True)
class ScanManifest:
    """Validated schema 1 manifest model."""

    schema_version: str
    application_version: str
    scan_id: UUID
    state: str
    started_at: str
    completed_at: str | None
    artifacts: Mapping[str, ArtifactEntry]
    counts: ScanCounts
    configuration: ScanConfiguration

    @classmethod
    def from_dict(
        cls,
        value: Any,
        *,
        location: str = "scan_manifest.json",
    ) -> "ScanManifest":
        data = _expect_mapping(value, location)
        _validate_keys(data, _MANIFEST_FIELDS, location)
        schema_version = _parse_schema_version(
            data["schema_version"], f"{location}.schema_version"
        )
        application_version = _expect_text(
            data["application_version"], f"{location}.application_version"
        )
        scan_id = _parse_uuid(data["scan_id"], f"{location}.scan_id", version=4)
        state = _expect_text(data["state"], f"{location}.state")
        if state not in MANIFEST_STATES:
            raise _error(
                f"{location}.state",
                f"must be one of: {', '.join(sorted(MANIFEST_STATES))}",
            )
        started_at = _parse_rfc3339_utc(data["started_at"], f"{location}.started_at")
        completed_value = data["completed_at"]
        if completed_value is None:
            completed_at = None
        else:
            completed_at = _parse_rfc3339_utc(
                completed_value, f"{location}.completed_at"
            )
        if state == "running" and completed_at is not None:
            raise _error(
                f"{location}.completed_at",
                "must be null while state is running",
            )
        if state != "running" and completed_at is None:
            raise _error(
                f"{location}.completed_at",
                "must be set for a final state",
            )
        if completed_at is not None and _rfc3339_order_key(
            completed_at
        ) < _rfc3339_order_key(started_at):
            raise _error(
                f"{location}.completed_at",
                "must be greater than or equal to started_at",
            )

        artifact_values = _expect_mapping(data["artifacts"], f"{location}.artifacts")
        artifacts = {
            name: ArtifactEntry.from_dict(
                name,
                artifact_value,
                location=f"{location}.artifacts.{name}",
            )
            for name, artifact_value in artifact_values.items()
        }
        counts = ScanCounts.from_dict(data["counts"], location=f"{location}.counts")
        configuration = ScanConfiguration.from_dict(
            data["configuration"], location=f"{location}.configuration"
        )
        manifest = cls(
            schema_version=schema_version,
            application_version=application_version,
            scan_id=scan_id,
            state=state,
            started_at=started_at,
            completed_at=completed_at,
            artifacts=artifacts,
            counts=counts,
            configuration=configuration,
        )
        manifest._validate_state_contract(location)
        return manifest

    def _validate_state_contract(self, location: str) -> None:
        if self.state in {"complete", "incomplete"}:
            missing = [
                name
                for name in ("library_scan", "scan_errors")
                if name not in self.artifacts
            ]
            if missing:
                raise _error(
                    f"{location}.artifacts",
                    f"final consumable scan is missing: {', '.join(missing)}",
                )
        if self.state == "complete":
            if self.counts.error_findings or self.counts.fatal_findings:
                raise _error(
                    f"{location}.counts",
                    "complete scans cannot contain error or fatal findings",
                )
        elif self.state == "incomplete":
            if self.counts.error_findings < 1:
                raise _error(
                    f"{location}.counts.error_findings",
                    "incomplete scans require at least one error finding",
                )
            if self.counts.fatal_findings:
                raise _error(
                    f"{location}.counts.fatal_findings",
                    "incomplete scans cannot contain fatal findings",
                )
        elif self.state == "failed":
            if self.counts.fatal_findings < 1:
                raise _error(
                    f"{location}.counts.fatal_findings",
                    "failed scans require at least one fatal finding",
                )
            if "library_scan" in self.artifacts:
                raise _error(
                    f"{location}.artifacts.library_scan",
                    "failed scans cannot register a consumable inventory",
                )

        if self.state in {"running", "failed"} and any(
            entry.role == "derived" for entry in self.artifacts.values()
        ):
            raise _error(
                f"{location}.artifacts",
                f"{self.state} scans cannot register derived artifacts",
            )

        library_entry = self.artifacts.get("library_scan")
        if (
            library_entry is not None
            and library_entry.row_count != self.counts.inventory_rows
        ):
            raise _error(
                f"{location}.artifacts.library_scan.row_count",
                "must equal counts.inventory_rows",
            )
        errors_entry = self.artifacts.get("scan_errors")
        if (
            errors_entry is not None
            and errors_entry.row_count != self.counts.finding_rows
        ):
            raise _error(
                f"{location}.artifacts.scan_errors.row_count",
                "must equal the total finding count",
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible representation."""
        return {
            "schema_version": self.schema_version,
            "application_version": self.application_version,
            "scan_id": str(self.scan_id),
            "state": self.state,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "artifacts": {
                name: artifact.to_dict() for name, artifact in self.artifacts.items()
            },
            "counts": self.counts.to_dict(),
            "configuration": self.configuration.to_dict(),
        }


@dataclass(frozen=True)
class LibraryScanRow:
    """One strictly validated schema 1 inventory row."""

    scan_id: UUID
    file_record_id: UUID
    file_fingerprint: str
    path: str
    extension: str
    file_type: str
    file_size_bytes: int | None
    modified_time_ns: int | None
    artist: str
    album_artist: str
    title: str
    album: str
    date: str
    release_year: int | None
    track_number: int | None
    track_total: int | None
    disc_number: int | None
    disc_total: int | None
    genre: str
    composer: str
    is_compilation: bool | None
    codec: str
    container: str
    bitrate_kbps: Decimal | None
    duration_seconds: Decimal | None
    sample_rate_hz: int | None
    bit_depth: int | None
    channels: int | None
    record_status: str

    @classmethod
    def from_csv_row(
        cls,
        value: Mapping[str, str],
        *,
        location: str = "library_scan.csv row",
    ) -> "LibraryScanRow":
        _validate_csv_row_mapping(value, LIBRARY_SCAN_HEADER, location)
        scan_id = _parse_uuid(value["scan_id"], f"{location}.scan_id", version=4)
        path = _validate_relative_path(value["path"], f"{location}.path")
        file_record_id = _parse_uuid(
            value["file_record_id"],
            f"{location}.file_record_id",
            version=5,
        )
        expected_record_id = make_file_record_id(scan_id, path)
        if file_record_id != expected_record_id:
            raise _error(
                f"{location}.file_record_id",
                "does not match scan_id and path",
            )

        extension = _parse_csv_text(
            value["extension"], f"{location}.extension", nullable=False
        )
        if _EXTENSION_RE.fullmatch(extension) is None:
            raise _error(
                f"{location}.extension",
                "must be a lowercase extension beginning with .",
            )
        file_type = value["file_type"]
        if file_type not in FILE_TYPES:
            raise _error(
                f"{location}.file_type",
                f"must be one of: {', '.join(sorted(FILE_TYPES))}",
            )
        if file_type == "archive" and extension != ".zip":
            raise _error(
                f"{location}.extension",
                "archive rows must use the .zip extension",
            )
        if file_type == "audio" and extension == ".zip":
            raise _error(
                f"{location}.extension",
                "audio rows cannot use the .zip extension",
            )

        file_size_bytes = _parse_csv_integer(
            value["file_size_bytes"],
            f"{location}.file_size_bytes",
            nullable=True,
            nonnegative=True,
        )
        modified_time_ns = _parse_csv_integer(
            value["modified_time_ns"],
            f"{location}.modified_time_ns",
            nullable=True,
            nonnegative=False,
        )
        if (file_size_bytes is None) != (modified_time_ns is None):
            raise _error(
                location,
                "file_size_bytes and modified_time_ns must both be set or empty",
            )

        fingerprint = value["file_fingerprint"]
        if file_size_bytes is None:
            if fingerprint != "":
                raise _error(
                    f"{location}.file_fingerprint",
                    "must be empty when stat data is unavailable",
                )
        else:
            if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
                raise _error(
                    f"{location}.file_fingerprint",
                    "must use stat-v1 with 64 lowercase hexadecimal digits",
                )
            if modified_time_ns is None:  # Guarded by the paired-null check.
                raise AssertionError("modified_time_ns must be present")
            expected_fingerprint = make_file_fingerprint(
                file_size_bytes, modified_time_ns
            )
            if fingerprint != expected_fingerprint:
                raise _error(
                    f"{location}.file_fingerprint",
                    "does not match file_size_bytes and modified_time_ns",
                )

        text_fields = {
            name: _parse_csv_text(value[name], f"{location}.{name}", nullable=True)
            for name in (
                "artist",
                "album_artist",
                "title",
                "album",
                "date",
                "genre",
                "composer",
                "codec",
                "container",
            )
        }
        optional_integers = {
            name: _parse_csv_integer(
                value[name],
                f"{location}.{name}",
                nullable=True,
                nonnegative=True,
            )
            for name in (
                "release_year",
                "track_number",
                "track_total",
                "disc_number",
                "disc_total",
                "sample_rate_hz",
                "bit_depth",
                "channels",
            )
        }
        is_compilation = _parse_csv_bool(
            value["is_compilation"],
            f"{location}.is_compilation",
            nullable=True,
        )
        bitrate_kbps = _parse_csv_decimal(
            value["bitrate_kbps"],
            f"{location}.bitrate_kbps",
            nullable=True,
        )
        duration_seconds = _parse_csv_decimal(
            value["duration_seconds"],
            f"{location}.duration_seconds",
            nullable=True,
        )
        record_status = value["record_status"]
        if record_status not in RECORD_STATUSES:
            raise _error(
                f"{location}.record_status",
                f"must be one of: {', '.join(sorted(RECORD_STATUSES))}",
            )
        if file_size_bytes is None and record_status != "error":
            raise _error(
                f"{location}.record_status",
                "must be error when stat data is unavailable",
            )

        return cls(
            scan_id=scan_id,
            file_record_id=file_record_id,
            file_fingerprint=fingerprint,
            path=path,
            extension=extension,
            file_type=file_type,
            file_size_bytes=file_size_bytes,
            modified_time_ns=modified_time_ns,
            artist=text_fields["artist"],
            album_artist=text_fields["album_artist"],
            title=text_fields["title"],
            album=text_fields["album"],
            date=text_fields["date"],
            release_year=optional_integers["release_year"],
            track_number=optional_integers["track_number"],
            track_total=optional_integers["track_total"],
            disc_number=optional_integers["disc_number"],
            disc_total=optional_integers["disc_total"],
            genre=text_fields["genre"],
            composer=text_fields["composer"],
            is_compilation=is_compilation,
            codec=text_fields["codec"],
            container=text_fields["container"],
            bitrate_kbps=bitrate_kbps,
            duration_seconds=duration_seconds,
            sample_rate_hz=optional_integers["sample_rate_hz"],
            bit_depth=optional_integers["bit_depth"],
            channels=optional_integers["channels"],
            record_status=record_status,
        )

    def to_csv_row(self) -> dict[str, str]:
        """Return the canonical string representation in schema header order."""

        def optional(value: object | None) -> str:
            return "" if value is None else str(value)

        return {
            "scan_id": str(self.scan_id),
            "file_record_id": str(self.file_record_id),
            "file_fingerprint": self.file_fingerprint,
            "path": self.path,
            "extension": self.extension,
            "file_type": self.file_type,
            "file_size_bytes": optional(self.file_size_bytes),
            "modified_time_ns": optional(self.modified_time_ns),
            "artist": self.artist,
            "album_artist": self.album_artist,
            "title": self.title,
            "album": self.album,
            "date": self.date,
            "release_year": optional(self.release_year),
            "track_number": optional(self.track_number),
            "track_total": optional(self.track_total),
            "disc_number": optional(self.disc_number),
            "disc_total": optional(self.disc_total),
            "genre": self.genre,
            "composer": self.composer,
            "is_compilation": (
                "" if self.is_compilation is None else str(self.is_compilation).lower()
            ),
            "codec": self.codec,
            "container": self.container,
            "bitrate_kbps": optional(self.bitrate_kbps),
            "duration_seconds": optional(self.duration_seconds),
            "sample_rate_hz": optional(self.sample_rate_hz),
            "bit_depth": optional(self.bit_depth),
            "channels": optional(self.channels),
            "record_status": self.record_status,
        }


@dataclass(frozen=True)
class ScanErrorRow:
    """One strictly validated schema 1 structured finding row."""

    scan_id: UUID
    file_record_id: UUID | None
    path: str
    stage: str
    severity: str
    error_code: str
    message: str

    @classmethod
    def from_csv_row(
        cls,
        value: Mapping[str, str],
        *,
        location: str = "scan_errors.csv row",
    ) -> "ScanErrorRow":
        _validate_csv_row_mapping(value, SCAN_ERRORS_HEADER, location)
        scan_id = _parse_uuid(value["scan_id"], f"{location}.scan_id", version=4)
        record_text = value["file_record_id"]
        file_record_id = (
            None
            if record_text == ""
            else _parse_uuid(record_text, f"{location}.file_record_id", version=5)
        )
        path = value["path"]
        if path:
            _validate_relative_path(path, f"{location}.path")
        if file_record_id is not None and path == "":
            raise _error(
                f"{location}.path",
                "must be present when file_record_id is present",
            )

        stage = value["stage"]
        if stage not in ERROR_STAGES:
            raise _error(
                f"{location}.stage",
                f"must be one of: {', '.join(sorted(ERROR_STAGES))}",
            )
        severity = value["severity"]
        if severity not in ERROR_SEVERITIES:
            raise _error(
                f"{location}.severity",
                f"must be one of: {', '.join(sorted(ERROR_SEVERITIES))}",
            )
        error_code = _parse_csv_text(
            value["error_code"], f"{location}.error_code", nullable=False
        )
        if _ERROR_CODE_RE.fullmatch(error_code) is None:
            raise _error(
                f"{location}.error_code",
                "must be lowercase snake case",
            )
        message = _parse_csv_text(
            value["message"], f"{location}.message", nullable=False
        )
        if error_code == "symlink_skipped":
            if severity != "info":
                raise _error(
                    f"{location}.severity",
                    "symlink_skipped findings must use info severity",
                )
            if path == "" or file_record_id is not None:
                raise _error(
                    location,
                    "symlink_skipped requires a path and no file_record_id",
                )
        return cls(
            scan_id=scan_id,
            file_record_id=file_record_id,
            path=path,
            stage=stage,
            severity=severity,
            error_code=error_code,
            message=message,
        )

    def to_csv_row(self) -> dict[str, str]:
        """Return the canonical string representation in schema header order."""
        return {
            "scan_id": str(self.scan_id),
            "file_record_id": (
                "" if self.file_record_id is None else str(self.file_record_id)
            ),
            "path": self.path,
            "stage": self.stage,
            "severity": self.severity,
            "error_code": self.error_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class ValidatedArtifactSet:
    """A manifest and every validated primary row registered by it."""

    manifest: ScanManifest
    library_rows: tuple[LibraryScanRow, ...]
    error_rows: tuple[ScanErrorRow, ...]


def _validate_header(
    actual: Sequence[str],
    expected: Sequence[str],
    location: str,
) -> None:
    duplicates = sorted(
        {name for name in actual if actual.count(name) > 1},
        key=str,
    )
    if duplicates:
        raise _error(location, f"duplicate columns: {', '.join(duplicates)}")
    missing = [name for name in expected if name not in actual]
    extra = [name for name in actual if name not in expected]
    if missing:
        raise _error(location, f"missing columns: {', '.join(missing)}")
    if extra:
        raise _error(location, f"unexpected columns: {', '.join(extra)}")
    if tuple(actual) != tuple(expected):
        raise _error(location, "columns are not in the required order")


def _load_csv_rows(
    path: Path,
    expected_header: Sequence[str],
    parser: Callable[[Mapping[str, str], str], _Row],
) -> tuple[_Row, ...]:
    try:
        report = path.open(encoding="utf-8", newline="")
    except (OSError, UnicodeError) as error:
        raise _error(str(path), f"cannot read artifact: {error}") from error
    with report:
        try:
            reader = csv.reader(report, strict=True)
            header = next(reader)
            _validate_header(header, expected_header, f"{path.name} header")
            rows: list[_Row] = []
            for row_number, cells in enumerate(reader, start=2):
                if len(cells) != len(expected_header):
                    raise _error(
                        f"{path.name} row {row_number}",
                        f"has {len(cells)} cells; expected {len(expected_header)}",
                    )
                row = dict(zip(expected_header, cells))
                rows.append(parser(row, f"{path.name} row {row_number}"))
            return tuple(rows)
        except StopIteration as error:
            raise _error(path.name, "missing header") from error
        except csv.Error as error:
            raise _error(path.name, f"malformed CSV: {error}") from error


def load_library_scan(path: Path) -> tuple[LibraryScanRow, ...]:
    """Load and validate a schema 1 library inventory without path access."""
    return _load_csv_rows(
        path,
        LIBRARY_SCAN_HEADER,
        lambda row, location: LibraryScanRow.from_csv_row(row, location=location),
    )


def load_scan_errors(path: Path) -> tuple[ScanErrorRow, ...]:
    """Load and validate a schema 1 structured finding report."""
    return _load_csv_rows(
        path,
        SCAN_ERRORS_HEADER,
        lambda row, location: ScanErrorRow.from_csv_row(row, location=location),
    )


def _reject_json_constant(value: str) -> None:
    raise ArtifactValidationError(f"scan_manifest.json: invalid JSON constant {value}")


def _json_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactValidationError(
                f"scan_manifest.json: duplicate JSON field {key!r}"
            )
        value[key] = item
    return value


def load_scan_manifest(path: Path) -> ScanManifest:
    """Load and validate one schema 1 manifest."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _error(str(path), f"cannot read artifact: {error}") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except ArtifactValidationError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise _error(path.name, f"malformed JSON: {error}") from error
    return ScanManifest.from_dict(value, location=path.name)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as artifact:
            for block in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise _error(str(path), f"cannot read artifact: {error}") from error
    return digest.hexdigest()


def _validate_artifact_digest(path: Path, entry: ArtifactEntry) -> None:
    if path.is_symlink():
        raise _error(str(path), "registered artifacts cannot be symlinks")
    actual_digest = _file_sha256(path)
    if actual_digest != entry.sha256:
        raise _error(str(path), "SHA-256 digest does not match the manifest")


def _validate_derived_csv(
    path: Path,
    entry: ArtifactEntry,
    scan_id: UUID,
) -> None:
    try:
        report = path.open(encoding="utf-8", newline="")
    except (OSError, UnicodeError) as error:
        raise _error(str(path), f"cannot read artifact: {error}") from error
    with report:
        try:
            reader = csv.reader(report, strict=True)
            header = next(reader)
            if not header or header[0] != "scan_id":
                raise _error(
                    f"{path.name} header",
                    "derived artifact must begin with scan_id",
                )
            duplicates = sorted(
                {name for name in header if header.count(name) > 1},
                key=str,
            )
            if duplicates:
                raise _error(
                    f"{path.name} header",
                    f"duplicate columns: {', '.join(duplicates)}",
                )
            row_count = 0
            for row_number, cells in enumerate(reader, start=2):
                if len(cells) != len(header):
                    raise _error(
                        f"{path.name} row {row_number}",
                        f"has {len(cells)} cells; expected {len(header)}",
                    )
                row_scan_id = _parse_uuid(
                    cells[0],
                    f"{path.name} row {row_number}.scan_id",
                    version=4,
                )
                if row_scan_id != scan_id:
                    raise _error(
                        f"{path.name} row {row_number}.scan_id",
                        "does not match the manifest",
                    )
                row_count += 1
        except StopIteration as error:
            raise _error(path.name, "missing header") from error
        except csv.Error as error:
            raise _error(path.name, f"malformed CSV: {error}") from error
    if row_count != entry.row_count:
        raise _error(
            path.name,
            f"row count {row_count} does not match manifest value {entry.row_count}",
        )


def validate_artifact_set(manifest_path: Path) -> ValidatedArtifactSet:
    """Validate registered artifacts without touching reported library paths."""
    manifest = load_scan_manifest(manifest_path)
    directory = manifest_path.parent
    library_rows: tuple[LibraryScanRow, ...] = ()
    error_rows: tuple[ScanErrorRow, ...] = ()

    for logical_name, entry in manifest.artifacts.items():
        artifact_path = directory / entry.filename
        _validate_artifact_digest(artifact_path, entry)
        if logical_name == "library_scan":
            library_rows = load_library_scan(artifact_path)
            actual_count = len(library_rows)
        elif logical_name == "scan_errors":
            error_rows = load_scan_errors(artifact_path)
            actual_count = len(error_rows)
        else:
            _validate_derived_csv(artifact_path, entry, manifest.scan_id)
            continue
        if actual_count != entry.row_count:
            raise _error(
                artifact_path.name,
                f"row count {actual_count} does not match manifest "
                f"value {entry.row_count}",
            )

    _validate_primary_relationships(manifest, library_rows, error_rows)
    return ValidatedArtifactSet(
        manifest=manifest,
        library_rows=library_rows,
        error_rows=error_rows,
    )


def _validate_primary_relationships(
    manifest: ScanManifest,
    library_rows: Sequence[LibraryScanRow],
    error_rows: Sequence[ScanErrorRow],
) -> None:
    for row_number, row in enumerate(library_rows, start=2):
        if row.scan_id != manifest.scan_id:
            raise _error(
                f"library_scan.csv row {row_number}.scan_id",
                "does not match the manifest",
            )
    for row_number, row in enumerate(error_rows, start=2):
        if row.scan_id != manifest.scan_id:
            raise _error(
                f"scan_errors.csv row {row_number}.scan_id",
                "does not match the manifest",
            )

    if "library_scan" in manifest.artifacts:
        if len(library_rows) != manifest.counts.inventory_rows:
            raise _error(
                "scan_manifest.json counts.inventory_rows",
                "does not match library_scan.csv",
            )
        records_by_id: dict[UUID, LibraryScanRow] = {}
        records_by_path: dict[str, LibraryScanRow] = {}
        for row in library_rows:
            if row.file_record_id in records_by_id:
                raise _error(
                    "library_scan.csv",
                    f"duplicate file_record_id {row.file_record_id}",
                )
            if row.path in records_by_path:
                raise _error("library_scan.csv", f"duplicate path {row.path!r}")
            records_by_id[row.file_record_id] = row
            records_by_path[row.path] = row

        findings_by_record: dict[UUID, list[ScanErrorRow]] = {}
        for row_number, finding in enumerate(error_rows, start=2):
            if finding.file_record_id is None:
                continue
            record = records_by_id.get(finding.file_record_id)
            if record is None:
                raise _error(
                    f"scan_errors.csv row {row_number}.file_record_id",
                    "does not reference an inventory row",
                )
            if finding.path != record.path:
                raise _error(
                    f"scan_errors.csv row {row_number}.path",
                    "does not match the referenced inventory row",
                )
            findings_by_record.setdefault(finding.file_record_id, []).append(finding)

        for row_number, record in enumerate(library_rows, start=2):
            linked = findings_by_record.get(record.file_record_id, [])
            if record.record_status == "error" and not any(
                finding.severity in {"error", "fatal"} for finding in linked
            ):
                raise _error(
                    f"library_scan.csv row {row_number}.record_status",
                    "error rows require a linked error or fatal finding",
                )
            if record.record_status == "ok" and any(
                finding.severity in {"error", "fatal"} for finding in linked
            ):
                raise _error(
                    f"library_scan.csv row {row_number}.record_status",
                    "must be error when linked to an error or fatal finding",
                )
            if record.file_size_bytes is None and not any(
                finding.stage == "stat" and finding.severity in {"error", "fatal"}
                for finding in linked
            ):
                raise _error(
                    f"library_scan.csv row {row_number}.file_size_bytes",
                    "missing stat data requires a linked stat finding",
                )

    if "scan_errors" in manifest.artifacts:
        severity_counts = {
            severity: sum(row.severity == severity for row in error_rows)
            for severity in ERROR_SEVERITIES
        }
        expected_counts = {
            "info": manifest.counts.info_findings,
            "error": manifest.counts.error_findings,
            "fatal": manifest.counts.fatal_findings,
        }
        if severity_counts != expected_counts:
            raise _error(
                "scan_manifest.json counts",
                "finding severities do not match scan_errors.csv",
            )
        skipped_symlinks = sum(
            row.error_code == "symlink_skipped" for row in error_rows
        )
        if skipped_symlinks != manifest.counts.skipped_symlinks:
            raise _error(
                "scan_manifest.json counts.skipped_symlinks",
                "does not match scan_errors.csv",
            )
