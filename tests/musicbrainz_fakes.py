"""Reusable offline MusicBrainz fakes for client-policy tests."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode
from uuid import RFC_4122, UUID

from music_manager.matcher import (
    MUSICBRAINZ_CANDIDATE_LIMIT,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
)


MUSICBRAINZ_ORIGIN = "https://musicbrainz.org"
MUSICBRAINZ_API_ROOT = "/ws/2/"
_LUCENE_SPECIAL = re.compile(r'(\&\&|\|\||[+\-!(){}\[\]^"~*?:\\/])')
_PARTIAL_DATE = re.compile(
    r"^[0-9]{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?)?$"
)


class FakeTransportError(RuntimeError):
    """Base error raised by the offline transport."""


class RedirectRejected(FakeTransportError):
    """Raised when a fake response attempts any redirect."""


class MalformedFakeResponse(FakeTransportError):
    """Raised when a fake response has no valid search-result envelope."""


@dataclass(frozen=True)
class FakeRequest:
    """One immutable request captured at the injected transport boundary."""

    method: str
    origin: str
    path: str
    headers: tuple[tuple[str, str], ...]
    parameters: tuple[tuple[str, str], ...]

    @property
    def encoded_url(self) -> str:
        """Return the exact encoded URL a future transport would receive."""
        return f"{self.origin}{self.path}?{urlencode(self.parameters)}"


@dataclass(frozen=True)
class FakeResponse:
    """One queued transport response with no HTTP-library dependency."""

    status: int
    payload: Any = None
    headers: tuple[tuple[str, str], ...] = ()


class FakeTransport:
    """Record requests and return queued synthetic responses."""

    def __init__(self, responses: Sequence[FakeResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[FakeRequest] = []

    def send(self, request: FakeRequest) -> Any:
        """Capture one request without DNS, sockets, or redirect following."""
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("fake transport response queue is empty")
        response = self._responses.pop(0)
        if 300 <= response.status <= 399:
            location = dict(response.headers).get("Location", "")
            raise RedirectRejected(f"redirect rejected: {location}")
        if response.status != 200:
            raise FakeTransportError(f"unexpected fake HTTP status {response.status}")
        return response.payload


@dataclass(frozen=True)
class FakeSearchCall:
    """Only the allowlisted local values accepted by the fake client."""

    operation: str
    artist: str
    title: str
    limit: int


class FakeMusicBrainzClient:
    """Offline policy reference implementing the application-owned protocol."""

    def __init__(self, *, user_agent: str, transport: FakeTransport) -> None:
        self._user_agent = user_agent
        self._transport = transport
        self.search_calls: list[FakeSearchCall] = []
        self.malformed_item_count = 0

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple[ReleaseGroupSearchResult, ...]:
        """Normalize an invented release-group response through a fake request."""
        self._record_call("release-group", album_artist, album_title, limit)
        query = _fielded_query(
            subject_field="releasegroup",
            subject_text=album_title,
            artist_text=album_artist,
        )
        payload = self._send("release-group", query, limit)
        items = _response_items(payload, "release-groups")
        return self._normalize_release_groups(items, limit)

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple[RecordingSearchResult, ...]:
        """Normalize an invented recording response through a fake request."""
        self._record_call("recording", track_artist, track_title, limit)
        query = _fielded_query(
            subject_field="recording",
            subject_text=track_title,
            artist_text=track_artist,
        )
        payload = self._send("recording", query, limit)
        items = _response_items(payload, "recordings")
        return self._normalize_recordings(items, limit)

    def _record_call(
        self,
        operation: str,
        artist: str,
        title: str,
        limit: int,
    ) -> None:
        if limit != MUSICBRAINZ_CANDIDATE_LIMIT:
            raise ValueError(
                f"fake MusicBrainz searches require limit {MUSICBRAINZ_CANDIDATE_LIMIT}"
            )
        self.search_calls.append(
            FakeSearchCall(
                operation=operation,
                artist=artist,
                title=title,
                limit=limit,
            )
        )

    def _send(self, resource: str, query: str, limit: int) -> Any:
        request = FakeRequest(
            method="GET",
            origin=MUSICBRAINZ_ORIGIN,
            path=f"{MUSICBRAINZ_API_ROOT}{resource}",
            headers=(
                ("Accept", "application/json"),
                ("User-Agent", self._user_agent),
            ),
            parameters=(
                ("fmt", "json"),
                ("limit", str(limit)),
                ("query", query),
            ),
        )
        return self._transport.send(request)

    def _normalize_release_groups(
        self,
        items: Sequence[Any],
        limit: int,
    ) -> tuple[ReleaseGroupSearchResult, ...]:
        normalized: list[ReleaseGroupSearchResult] = []
        for item in items:
            try:
                normalized.append(_release_group_result(item))
            except (TypeError, ValueError):
                self.malformed_item_count += 1
        return _deduplicate(normalized, limit)

    def _normalize_recordings(
        self,
        items: Sequence[Any],
        limit: int,
    ) -> tuple[RecordingSearchResult, ...]:
        normalized: list[RecordingSearchResult] = []
        for item in items:
            try:
                normalized.append(_recording_result(item))
            except (TypeError, ValueError):
                self.malformed_item_count += 1
        return _deduplicate(normalized, limit)


def _response_items(payload: Any, field_name: str) -> Sequence[Any]:
    if not isinstance(payload, dict):
        raise MalformedFakeResponse("response body must be an object")
    items = payload.get(field_name)
    if not isinstance(items, list):
        raise MalformedFakeResponse(f"response field {field_name!r} must be a list")
    return items


def _fielded_query(
    *,
    subject_field: str,
    subject_text: str,
    artist_text: str,
) -> str:
    subject = _escape_lucene(_query_text(subject_text))
    artist = _escape_lucene(_query_text(artist_text))
    return f'{subject_field}:"{subject}" AND artist:"{artist}"'


def _query_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("query values must be text")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("query values must not be empty")
    return normalized


def _escape_lucene(value: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", value)


def _release_group_result(item: Any) -> ReleaseGroupSearchResult:
    value = _mapping(item)
    return ReleaseGroupSearchResult(
        mbid=_canonical_mbid(value.get("id")),
        title=_required_text(value.get("title")),
        artist_credit=_artist_credit(value.get("artist-credit")),
        first_release_date=_optional_date(value.get("first-release-date")),
        primary_type=_optional_text(value.get("primary-type")),
        secondary_types=_text_tuple(value.get("secondary-types")),
        search_score=_search_score(value.get("score", 0)),
    )


def _recording_result(item: Any) -> RecordingSearchResult:
    value = _mapping(item)
    duration = value.get("length")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration < 0
    ):
        raise ValueError("recording length must be a non-negative integer")
    return RecordingSearchResult(
        mbid=_canonical_mbid(value.get("id")),
        title=_required_text(value.get("title")),
        artist_credit=_artist_credit(value.get("artist-credit")),
        duration_ms=duration,
        first_release_date=_optional_date(value.get("first-release-date")),
        releases=_releases(value.get("releases")),
        search_score=_search_score(value.get("score", 0)),
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise TypeError("response item must be an object")
    return value


def _canonical_mbid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise TypeError("MusicBrainz identifier must be text")
    parsed = UUID(value)
    if str(parsed) != value or parsed.variant != RFC_4122:
        raise ValueError("MusicBrainz identifier must be a canonical UUID")
    return parsed


def _candidate_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip()


def _required_text(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("required display text must be text")
    normalized = _candidate_text(value)
    if not normalized:
        raise ValueError("required display text must not be empty")
    return normalized


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError("optional display text must be text")
    return _candidate_text(value)


def _optional_date(value: Any) -> str:
    date = _optional_text(value)
    if date and _PARTIAL_DATE.fullmatch(date) is None:
        raise ValueError("date must use YYYY, YYYY-MM, or YYYY-MM-DD form")
    return date


def _artist_credit(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, list):
        raise TypeError("artist-credit must be a list")
    parts: list[str] = []
    for credit in value:
        credit_value = _mapping(credit)
        parts.append(_required_text(credit_value.get("name")))
        joinphrase = credit_value.get("joinphrase", "")
        if not isinstance(joinphrase, str):
            raise TypeError("artist-credit joinphrase must be text")
        parts.append(unicodedata.normalize("NFKC", joinphrase))
    return _candidate_text("".join(parts))


def _text_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("text collection must be a list")
    return tuple(sorted({_required_text(item) for item in value}))


def _search_score(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("search score must be an integer from 0 through 100")
    return value


def _releases(value: Any) -> tuple[tuple[UUID, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TypeError("releases must be a list")
    releases = {
        (_canonical_mbid(_mapping(item).get("id")), _required_text(item.get("title")))
        for item in value
    }
    return tuple(sorted(releases, key=lambda release: (str(release[0]), release[1])))


def _deduplicate(
    values: Sequence[ReleaseGroupSearchResult | RecordingSearchResult],
    limit: int,
) -> tuple[ReleaseGroupSearchResult | RecordingSearchResult, ...]:
    selected: dict[UUID, ReleaseGroupSearchResult | RecordingSearchResult] = {}
    for value in values:
        current = selected.get(value.mbid)
        if current is None or _selection_key(value) < _selection_key(current):
            selected[value.mbid] = value
    return tuple(sorted(selected.values(), key=_selection_key)[:limit])


def _selection_key(
    value: ReleaseGroupSearchResult | RecordingSearchResult,
) -> tuple:
    if isinstance(value, ReleaseGroupSearchResult):
        candidate_fields: tuple[Any, ...] = (
            value.title,
            value.artist_credit,
            value.first_release_date,
            value.primary_type,
            value.secondary_types,
        )
    else:
        candidate_fields = (
            value.title,
            value.artist_credit,
            value.duration_ms,
            value.first_release_date,
            value.releases,
        )
    fields = tuple(_normalized_field(field) for field in candidate_fields)
    return (-value.search_score, fields, str(value.mbid))


def _normalized_field(value: Any) -> str:
    if isinstance(value, tuple):
        return repr(tuple(_normalized_field(item) for item in value))
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()
