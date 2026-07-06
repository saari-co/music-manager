"""Production MusicBrainz client shell with injected, offline-testable policy."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlencode
from uuid import RFC_4122, UUID

from music_manager.matcher import (
    MUSICBRAINZ_APPLICATION_NAME,
    MUSICBRAINZ_CANDIDATE_LIMIT,
    MUSICBRAINZ_CONTACT_URL,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
)


MUSICBRAINZ_ORIGIN = "https://musicbrainz.org"
MUSICBRAINZ_API_ROOT = "/ws/2/"
MUSICBRAINZ_RESPONSE_FORMAT = "json"
MUSICBRAINZ_CLIENT_POLICY_VERSION = "musicbrainz-client-v1"
MUSICBRAINZ_TIMEOUT_SECONDS = 30.0
MUSICBRAINZ_RATE_INTERVAL_SECONDS = 1.1
MUSICBRAINZ_CACHE_MAX_AGE_SECONDS = 2_592_000.0
MUSICBRAINZ_CACHE_FILENAME = "musicbrainz-v1.sqlite3"
MUSICBRAINZ_CACHE_SCHEMA_VERSION = 1
MUSICBRAINZ_MAX_ATTEMPTS = 4
MUSICBRAINZ_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
MUSICBRAINZ_MAX_RETRY_AFTER_SECONDS = 60.0
MUSICBRAINZ_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_LUCENE_SPECIAL = re.compile(r'(\&\&|\|\||[+\-!(){}\[\]^"~*?:\\/])')
_CACHE_KEY = re.compile(r"^[0-9a-f]{64}$")


class MusicBrainzClientError(RuntimeError):
    """Base class for sanitized production client failures."""


class MusicBrainzTransportError(MusicBrainzClientError):
    """A connection, timeout, or response-read failure."""


class MusicBrainzCacheError(MusicBrainzClientError):
    """A persistent cache initialization or write failure."""


class MusicBrainzRequestError(MusicBrainzClientError):
    """A non-retryable HTTP request failure."""


class MusicBrainzRedirectError(MusicBrainzRequestError):
    """An HTTP redirect rejected by policy."""


class MusicBrainzResponseError(MusicBrainzClientError):
    """A non-retryable decoded response contract violation."""


class MusicBrainzRetryExhausted(MusicBrainzClientError):
    """A retryable operation failed on every permitted attempt."""


class MusicBrainzCircuitOpen(MusicBrainzClientError):
    """The run-level circuit prevents another uncached live request."""


@dataclass(frozen=True)
class MusicBrainzRequest:
    """One fixed-origin request passed to an injected transport."""

    operation: str
    method: str
    origin: str
    path: str
    headers: tuple[tuple[str, str], ...]
    parameters: tuple[tuple[str, str], ...]
    timeout: float

    @property
    def encoded_url(self) -> str:
        """Return the URL encoded only at the transport boundary."""
        return f"{self.origin}{self.path}?{urlencode(self.parameters)}"


@dataclass(frozen=True)
class MusicBrainzTransportResponse:
    """HTTP-library-independent response returned by a transport."""

    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    complete: bool = True


@runtime_checkable
class MusicBrainzTransport(Protocol):
    """Injected transport interface used by the production client."""

    def send(self, request: MusicBrainzRequest) -> MusicBrainzTransportResponse:
        """Perform one request without following redirects."""


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        request: urllib.request.Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        return None


class UrllibMusicBrainzTransport:
    """Standard-library HTTPS transport with redirects disabled."""

    def __init__(self, opener: Any | None = None) -> None:
        self._opener = opener or urllib.request.build_opener(_RejectRedirectHandler())

    def send(self, request: MusicBrainzRequest) -> MusicBrainzTransportResponse:
        """Send one fixed-policy GET and sanitize transport failures."""
        if (
            request.method != "GET"
            or request.origin != MUSICBRAINZ_ORIGIN
            or not request.path.startswith(MUSICBRAINZ_API_ROOT)
            or not request.encoded_url.startswith(f"{MUSICBRAINZ_ORIGIN}/")
        ):
            raise MusicBrainzTransportError("MusicBrainz request policy violation")
        library_request = urllib.request.Request(
            request.encoded_url,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with self._opener.open(
                library_request,
                timeout=request.timeout,
            ) as response:
                return _transport_response(response)
        except urllib.error.HTTPError as error:
            try:
                body = error.read()
            except Exception:
                body = b""
            headers = tuple(error.headers.items()) if error.headers else ()
            return MusicBrainzTransportResponse(
                status=error.code,
                headers=headers,
                body=body,
                complete=_content_length_is_complete(headers, body),
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            OSError,
            http.client.HTTPException,
        ):
            raise MusicBrainzTransportError("MusicBrainz transport failure") from None


def _transport_response(response: Any) -> MusicBrainzTransportResponse:
    try:
        body = response.read()
        headers = tuple(response.headers.items())
        status = int(response.status)
    except (
        TimeoutError,
        socket.timeout,
        OSError,
        http.client.HTTPException,
    ):
        raise MusicBrainzTransportError("MusicBrainz response read failure") from None
    return MusicBrainzTransportResponse(
        status=status,
        headers=headers,
        body=body,
        complete=_content_length_is_complete(headers, body),
    )


def _content_length_is_complete(
    headers: Sequence[tuple[str, str]],
    body: bytes,
) -> bool:
    value = _header_value(headers, "Content-Length")
    if value is None:
        return True
    try:
        expected = int(value)
    except ValueError:
        return False
    return expected >= 0 and len(body) == expected


def default_musicbrainz_cache_path() -> Path:
    """Return the operating system's per-user v1 MusicBrainz cache path."""
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Caches"
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / MUSICBRAINZ_APPLICATION_NAME / MUSICBRAINZ_CACHE_FILENAME


class MusicBrainzCache:
    """Versioned SQLite cache containing opaque keys and public response bodies."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_musicbrainz_cache_path()
        self._lock = threading.RLock()
        self._connection = self._open()

    def _open(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise MusicBrainzCacheError("MusicBrainz cache path is unsafe")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _restrict_permissions(self.path.parent, 0o700)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            self._initialize(connection)
            _restrict_permissions(self.path, 0o600)
            return connection
        except (OSError, sqlite3.DatabaseError):
            raise MusicBrainzCacheError(
                "MusicBrainz cache initialization failed"
            ) from None

    def _initialize(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, MUSICBRAINZ_CACHE_SCHEMA_VERSION}:
            raise sqlite3.DatabaseError("unsupported cache schema")
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS responses (
                    cache_key TEXT PRIMARY KEY,
                    stored_at REAL NOT NULL,
                    body BLOB NOT NULL
                )
                """
            )
            connection.execute(
                f"PRAGMA user_version = {MUSICBRAINZ_CACHE_SCHEMA_VERSION}"
            )

    def get(self, cache_key: str, *, now: float) -> bytes | None:
        """Return one fresh body; corrupt and expired rows are misses."""
        _validate_cache_key(cache_key)
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT stored_at, body FROM responses WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
            except sqlite3.DatabaseError:
                return None
            if row is None:
                return None
            stored_at, body = row
            if (
                isinstance(stored_at, bool)
                or not isinstance(stored_at, (int, float))
                or not isinstance(body, bytes)
                or now < float(stored_at)
                or now - float(stored_at) > MUSICBRAINZ_CACHE_MAX_AGE_SECONDS
            ):
                self.delete(cache_key)
                return None
            return body

    def put(self, cache_key: str, *, stored_at: float, body: bytes) -> None:
        """Transactionally replace one successful response."""
        _validate_cache_key(cache_key)
        if (
            isinstance(stored_at, bool)
            or not isinstance(stored_at, (int, float))
            or not isinstance(body, bytes)
        ):
            raise TypeError("invalid MusicBrainz cache entry")
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        """
                        INSERT INTO responses(cache_key, stored_at, body)
                        VALUES (?, ?, ?)
                        ON CONFLICT(cache_key) DO UPDATE SET
                            stored_at = excluded.stored_at,
                            body = excluded.body
                        """,
                        (cache_key, float(stored_at), body),
                    )
            except sqlite3.DatabaseError:
                raise MusicBrainzCacheError("MusicBrainz cache write failed") from None

    def delete(self, cache_key: str) -> None:
        """Remove one unusable row without exposing its request."""
        _validate_cache_key(cache_key)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "DELETE FROM responses WHERE cache_key = ?",
                        (cache_key,),
                    )
            except sqlite3.DatabaseError:
                return

    def close(self) -> None:
        """Close the persistent connection."""
        with self._lock:
            self._connection.close()


def _restrict_permissions(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _validate_cache_key(value: str) -> None:
    if not isinstance(value, str) or _CACHE_KEY.fullmatch(value) is None:
        raise ValueError("MusicBrainz cache key must be a SHA-256 digest")


def build_musicbrainz_cache_key(request: MusicBrainzRequest) -> str:
    """Hash canonical request policy without retaining raw query text."""
    encoded_parameters = urlencode(sorted(request.parameters))
    canonical = "\n".join(
        (
            request.origin,
            request.path,
            MUSICBRAINZ_RESPONSE_FORMAT,
            encoded_parameters,
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProductionMusicBrainzClient:
    """Cached, rate-limited MusicBrainz client behind application value objects."""

    def __init__(
        self,
        *,
        user_agent: str,
        transport: MusicBrainzTransport | None = None,
        cache: MusicBrainzCache | None = None,
        cache_path: Path | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_user_agent(user_agent)
        if cache is not None and cache_path is not None:
            raise ValueError("provide a MusicBrainz cache or cache path, not both")
        self._user_agent = user_agent
        self._transport = transport or UrllibMusicBrainzTransport()
        self._cache = cache or MusicBrainzCache(cache_path)
        self._owns_cache = cache is None
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._sleeper = sleeper
        self._operation_lock = threading.RLock()
        self._last_live_start: float | None = None
        self._circuit_open = False
        self.malformed_item_count = 0

    @property
    def circuit_open(self) -> bool:
        """Whether retry exhaustion has disabled uncached live requests."""
        return self._circuit_open

    def close(self) -> None:
        """Close a cache created by this client."""
        if self._owns_cache:
            self._cache.close()

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple[ReleaseGroupSearchResult, ...]:
        """Search release groups and return deterministic boundary values."""
        return self._search(
            operation="release-group",
            subject_field="releasegroup",
            artist=album_artist,
            title=album_title,
            limit=limit,
            response_field="release-groups",
        )

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple[RecordingSearchResult, ...]:
        """Search recordings and return deterministic boundary values."""
        return self._search(
            operation="recording",
            subject_field="recording",
            artist=track_artist,
            title=track_title,
            limit=limit,
            response_field="recordings",
        )

    def _search(
        self,
        *,
        operation: str,
        subject_field: str,
        artist: str,
        title: str,
        limit: int,
        response_field: str,
    ) -> tuple:
        if limit != MUSICBRAINZ_CANDIDATE_LIMIT:
            raise ValueError(
                f"MusicBrainz searches require limit {MUSICBRAINZ_CANDIDATE_LIMIT}"
            )
        query = _fielded_query(
            subject_field=subject_field,
            subject_text=title,
            artist_text=artist,
        )
        request = MusicBrainzRequest(
            operation=operation,
            method="GET",
            origin=MUSICBRAINZ_ORIGIN,
            path=f"{MUSICBRAINZ_API_ROOT}{operation}",
            headers=(
                ("Accept", "application/json"),
                ("User-Agent", self._user_agent),
            ),
            parameters=(
                ("fmt", MUSICBRAINZ_RESPONSE_FORMAT),
                ("limit", str(limit)),
                ("query", query),
            ),
            timeout=MUSICBRAINZ_TIMEOUT_SECONDS,
        )
        cache_key = build_musicbrainz_cache_key(request)
        with self._operation_lock:
            cached_body = self._cache.get(cache_key, now=self._wall_clock())
            if cached_body is not None:
                try:
                    return self._decode_and_normalize(
                        cached_body,
                        operation=operation,
                        response_field=response_field,
                        limit=limit,
                    )
                except MusicBrainzClientError:
                    self._cache.delete(cache_key)
            if self._circuit_open:
                raise MusicBrainzCircuitOpen(
                    f"MusicBrainz {operation} request blocked by open circuit"
                )
            return self._live_search(
                request,
                cache_key=cache_key,
                response_field=response_field,
                limit=limit,
            )

    def _live_search(
        self,
        request: MusicBrainzRequest,
        *,
        cache_key: str,
        response_field: str,
        limit: int,
    ) -> tuple:
        retry_after = 0.0
        for attempt in range(1, MUSICBRAINZ_MAX_ATTEMPTS + 1):
            backoff = (
                0.0 if attempt == 1 else MUSICBRAINZ_RETRY_BACKOFF_SECONDS[attempt - 2]
            )
            self._wait_for_live_attempt(max(backoff, retry_after))
            retry_after = 0.0
            response_completed_at: float | None = None
            try:
                response = self._transport.send(request)
                response_completed_at = self._wall_clock()
            except MusicBrainzTransportError:
                if attempt == MUSICBRAINZ_MAX_ATTEMPTS:
                    self._open_circuit_and_raise(request.operation)
                continue
            except (TimeoutError, socket.timeout, OSError):
                if attempt == MUSICBRAINZ_MAX_ATTEMPTS:
                    self._open_circuit_and_raise(request.operation)
                continue

            if not response.complete:
                if attempt == MUSICBRAINZ_MAX_ATTEMPTS:
                    self._open_circuit_and_raise(request.operation)
                continue
            if 300 <= response.status <= 399:
                raise MusicBrainzRedirectError(
                    f"MusicBrainz {request.operation} redirect rejected"
                )
            if response.status in MUSICBRAINZ_RETRYABLE_STATUSES:
                retry_after = _retry_after_seconds(
                    response.headers,
                    wall_time=self._wall_clock(),
                )
                if retry_after > MUSICBRAINZ_MAX_RETRY_AFTER_SECONDS:
                    self._open_circuit_and_raise(
                        request.operation,
                        attempts=attempt,
                    )
                if attempt == MUSICBRAINZ_MAX_ATTEMPTS:
                    self._open_circuit_and_raise(request.operation)
                continue
            if response.status != 200:
                raise MusicBrainzRequestError(
                    f"MusicBrainz {request.operation} request failed "
                    f"with HTTP status {response.status}"
                )
            try:
                values = self._decode_and_normalize(
                    response.body,
                    operation=request.operation,
                    response_field=response_field,
                    limit=limit,
                )
            except _MalformedJson:
                if attempt == MUSICBRAINZ_MAX_ATTEMPTS:
                    self._open_circuit_and_raise(request.operation)
                continue
            self._cache.put(
                cache_key,
                stored_at=response_completed_at,
                body=response.body,
            )
            return values
        raise AssertionError("MusicBrainz retry loop must return or raise")

    def _wait_for_live_attempt(self, minimum_delay: float) -> None:
        now = self._monotonic_clock()
        rate_delay = (
            0.0
            if self._last_live_start is None
            else max(
                0.0,
                self._last_live_start + MUSICBRAINZ_RATE_INTERVAL_SECONDS - now,
            )
        )
        delay = max(0.0, minimum_delay, rate_delay)
        if delay:
            self._sleeper(delay)
        self._last_live_start = self._monotonic_clock()

    def _decode_and_normalize(
        self,
        body: bytes,
        *,
        operation: str,
        response_field: str,
        limit: int,
    ) -> tuple:
        payload = _decode_json(body)
        items = _response_items(
            payload,
            response_field=response_field,
            operation=operation,
        )
        normalized: list[ReleaseGroupSearchResult | RecordingSearchResult] = []
        for item in items:
            try:
                if operation == "release-group":
                    normalized.append(_release_group_result(item))
                else:
                    normalized.append(_recording_result(item))
            except (TypeError, ValueError):
                self.malformed_item_count += 1
        if items and not normalized:
            raise MusicBrainzResponseError(
                f"MusicBrainz {operation} response contained no valid items"
            )
        return _deduplicate(normalized, limit)

    def _open_circuit_and_raise(
        self,
        operation: str,
        *,
        attempts: int = MUSICBRAINZ_MAX_ATTEMPTS,
    ) -> None:
        self._circuit_open = True
        attempt_label = "attempt" if attempts == 1 else "attempts"
        raise MusicBrainzRetryExhausted(
            f"MusicBrainz {operation} retry policy stopped after "
            f"{attempts} {attempt_label}"
        ) from None


def _validate_user_agent(value: str) -> None:
    prefix = f"{MUSICBRAINZ_APPLICATION_NAME}/"
    suffix = f" ({MUSICBRAINZ_CONTACT_URL})"
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or not value.endswith(suffix)
    ):
        raise ValueError("invalid MusicBrainz User-Agent")
    version = value[len(prefix) : -len(suffix)]
    if not version or any(character.isspace() for character in version):
        raise ValueError("invalid MusicBrainz User-Agent")


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
        raise TypeError("MusicBrainz query values must be text")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        raise ValueError("MusicBrainz query values must not be empty")
    return normalized


def _escape_lucene(value: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", value)


class _MalformedJson(MusicBrainzClientError):
    pass


def _decode_json(body: bytes) -> Any:
    if not isinstance(body, bytes):
        raise _MalformedJson("MusicBrainz response was not bytes")
    try:
        return json.loads(
            body.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _MalformedJson("MusicBrainz response JSON was malformed") from None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _response_items(
    payload: Any,
    *,
    response_field: str,
    operation: str,
) -> Sequence[Any]:
    if not isinstance(payload, dict) or not isinstance(
        payload.get(response_field),
        list,
    ):
        raise MusicBrainzResponseError(
            f"MusicBrainz {operation} response contract violation"
        )
    return payload[response_field]


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
    text = _optional_text(value)
    if not text:
        return ""
    parts = text.split("-")
    if len(parts) not in {1, 2, 3} or any(
        not part.isascii() or not part.isdigit() for part in parts
    ):
        raise ValueError("invalid MusicBrainz partial date")
    if len(parts[0]) != 4 or any(len(part) != 2 for part in parts[1:]):
        raise ValueError("invalid MusicBrainz partial date")
    year = int(parts[0])
    month = int(parts[1]) if len(parts) >= 2 else 1
    day = int(parts[2]) if len(parts) == 3 else 1
    date(year, month, day)
    return text


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


def _retry_after_seconds(
    headers: Sequence[tuple[str, str]],
    *,
    wall_time: float,
) -> float:
    value = _header_value(headers, "Retry-After")
    if value is None:
        return 0.0
    stripped = value.strip()
    if stripped.isascii() and stripped.isdigit():
        return float(int(stripped))
    try:
        parsed = parsedate_to_datetime(stripped)
        if parsed.tzinfo is None:
            return 0.0
        return max(0.0, parsed.timestamp() - wall_time)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _header_value(
    headers: Sequence[tuple[str, str]],
    name: str,
) -> str | None:
    expected = name.casefold()
    for header_name, value in headers:
        if header_name.casefold() == expected:
            return value
    return None
