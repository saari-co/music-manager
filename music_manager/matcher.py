"""Opt-in MusicBrainz boundary without network or matching behavior."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable
from uuid import RFC_4122, UUID

from music_manager import __version__
from music_manager.artifact_schema import (
    ArtifactValidationError,
    ValidatedArtifactSet,
    validate_artifact_set,
)


MUSICBRAINZ_CONTACT_URL = "https://github.com/saari-co/music-manager"
MUSICBRAINZ_APPLICATION_NAME = "music-manager"
MUSICBRAINZ_CANDIDATE_LIMIT = 10
CONSENT_SOURCES = frozenset({"cli", "config"})


class MusicBrainzBoundaryError(ValueError):
    """Base error for the pre-network MusicBrainz boundary."""


class MusicBrainzConsentRequired(MusicBrainzBoundaryError):
    """Raised before artifact, cache, client, or transport access."""


@dataclass(frozen=True)
class ReleaseGroupSearchResult:
    """Validated client-boundary value for one MusicBrainz release group."""

    mbid: UUID
    title: str
    artist_credit: str
    first_release_date: str = ""
    primary_type: str = ""
    secondary_types: tuple[str, ...] = ()
    search_score: int = 0

    def __post_init__(self) -> None:
        _validate_search_result(
            self.mbid,
            self.title,
            self.artist_credit,
            self.search_score,
        )
        if tuple(sorted(set(self.secondary_types))) != self.secondary_types:
            raise ValueError("secondary_types must be unique and lexically sorted")


@dataclass(frozen=True)
class RecordingSearchResult:
    """Validated client-boundary value for one MusicBrainz recording."""

    mbid: UUID
    title: str
    artist_credit: str
    duration_ms: int | None = None
    first_release_date: str = ""
    releases: tuple[tuple[UUID, str], ...] = ()
    search_score: int = 0

    def __post_init__(self) -> None:
        _validate_search_result(
            self.mbid,
            self.title,
            self.artist_credit,
            self.search_score,
        )
        if self.duration_ms is not None and (
            isinstance(self.duration_ms, bool)
            or not isinstance(self.duration_ms, int)
            or self.duration_ms < 0
        ):
            raise ValueError("duration_ms must be a non-negative integer or None")
        for release_mbid, release_title in self.releases:
            _validate_mbid(release_mbid)
            _validate_text(release_title, field_name="release title", nullable=False)


@runtime_checkable
class MusicBrainzClient(Protocol):
    """Application-owned interface implemented by future real and fake clients."""

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> Sequence[ReleaseGroupSearchResult]:
        """Return release-group search values without exposing transport types."""

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> Sequence[RecordingSearchResult]:
        """Return recording search values without exposing transport types."""


class MusicBrainzClientFactory(Protocol):
    """Deferred factory; future cache and transport setup begins behind this call."""

    def __call__(self, *, user_agent: str) -> MusicBrainzClient:
        """Create a client only after consent and scan validation succeed."""


@dataclass(frozen=True)
class MusicBrainzPreflight:
    """Validated consent and scan context created before any client access."""

    run_directory: Path
    artifacts: ValidatedArtifactSet
    user_agent: str
    consent_source: str


@dataclass(frozen=True)
class InitializedMusicBrainzBoundary:
    """A validated preflight paired with an injected client implementation."""

    preflight: MusicBrainzPreflight
    client: MusicBrainzClient


def build_musicbrainz_user_agent(
    application_version: str = __version__,
) -> str:
    """Return the contract-required identifiable MusicBrainz User-Agent."""
    if (
        not application_version
        or application_version != application_version.strip()
        or any(character.isspace() for character in application_version)
    ):
        raise ValueError("application version must be non-empty without whitespace")
    return (
        f"{MUSICBRAINZ_APPLICATION_NAME}/{application_version} "
        f"({MUSICBRAINZ_CONTACT_URL})"
    )


def prepare_musicbrainz_preflight(
    run_directory: Path,
    *,
    enabled: bool,
    consent_source: str,
) -> MusicBrainzPreflight:
    """Validate consent and one schema 1 run without client or cache access."""
    if not enabled:
        raise MusicBrainzConsentRequired(
            "MusicBrainz is disabled; use --musicbrainz or set "
            "musicbrainz.enabled: true"
        )
    if consent_source not in CONSENT_SOURCES:
        raise MusicBrainzBoundaryError(
            "enabled MusicBrainz consent source must be 'cli' or 'config'"
        )
    if run_directory.is_symlink():
        raise ArtifactValidationError("scan run directory cannot be a symlink")
    if not run_directory.is_dir():
        raise ArtifactValidationError(
            f"scan run directory does not exist: {run_directory}"
        )

    artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
    manifest = artifacts.manifest
    if run_directory.name != str(manifest.scan_id):
        raise ArtifactValidationError(
            "scan run directory name does not match the manifest scan_id"
        )
    if manifest.state not in {"complete", "incomplete"}:
        raise ArtifactValidationError(
            f"scan state {manifest.state!r} cannot be matched"
        )
    return MusicBrainzPreflight(
        run_directory=run_directory,
        artifacts=artifacts,
        user_agent=build_musicbrainz_user_agent(),
        consent_source=consent_source,
    )


def open_musicbrainz_client_boundary(
    run_directory: Path,
    *,
    enabled: bool,
    consent_source: str,
    client_factory: MusicBrainzClientFactory,
) -> InitializedMusicBrainzBoundary:
    """Create an injected client only after all preflight checks pass."""
    preflight = prepare_musicbrainz_preflight(
        run_directory,
        enabled=enabled,
        consent_source=consent_source,
    )
    client = client_factory(user_agent=preflight.user_agent)
    return InitializedMusicBrainzBoundary(
        preflight=preflight,
        client=client,
    )


def _validate_search_result(
    mbid: UUID,
    title: str,
    artist_credit: str,
    search_score: int,
) -> None:
    _validate_mbid(mbid)
    _validate_text(title, field_name="title", nullable=False)
    _validate_text(artist_credit, field_name="artist_credit", nullable=True)
    if (
        isinstance(search_score, bool)
        or not isinstance(search_score, int)
        or not 0 <= search_score <= 100
    ):
        raise ValueError("search_score must be an integer from 0 through 100")


def _validate_mbid(value: UUID) -> None:
    if not isinstance(value, UUID) or value.variant != RFC_4122:
        raise ValueError("MusicBrainz identifiers must be RFC 4122 UUIDs")


def _validate_text(value: str, *, field_name: str, nullable: bool) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    if not nullable and value == "":
        raise ValueError(f"{field_name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain surrounding whitespace")
