"""Fully offline tests for the production MusicBrainz client shell."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat
import tempfile
import time
import unittest
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import format_datetime
from email.message import Message
from io import BytesIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from music_manager.matcher import (
    MUSICBRAINZ_CANDIDATE_LIMIT,
    MusicBrainzClient,
    RecordingSearchResult,
    ReleaseGroupSearchResult,
    build_musicbrainz_user_agent,
)
from music_manager.musicbrainz_client import (
    MUSICBRAINZ_API_ROOT,
    MUSICBRAINZ_CACHE_MAX_AGE_SECONDS,
    MUSICBRAINZ_CACHE_SCHEMA_VERSION,
    MUSICBRAINZ_MAX_ATTEMPTS,
    MUSICBRAINZ_ORIGIN,
    MUSICBRAINZ_RATE_INTERVAL_SECONDS,
    MUSICBRAINZ_TIMEOUT_SECONDS,
    MusicBrainzCache,
    MusicBrainzCircuitOpen,
    MusicBrainzRedirectError,
    MusicBrainzRequestError,
    MusicBrainzResponseError,
    MusicBrainzRetryExhausted,
    MusicBrainzTransportError,
    MusicBrainzTransportResponse,
    ProductionMusicBrainzClient,
    UrllibMusicBrainzTransport,
    build_musicbrainz_cache_key,
    default_musicbrainz_cache_path,
)


FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"
USER_AGENT = build_musicbrainz_user_agent("0.4.0")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _response(
    payload: object = None,
    *,
    status: int = 200,
    headers: tuple[tuple[str, str], ...] = (),
    complete: bool = True,
    body: bytes | None = None,
) -> MusicBrainzTransportResponse:
    encoded = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if body is None
        else body
    )
    return MusicBrainzTransportResponse(
        status=status,
        headers=headers,
        body=encoded,
        complete=complete,
    )


@dataclass
class _FakeClock:
    monotonic_value: float = 0.0
    wall_value: float = 1_800_000_000.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.monotonic_value

    def wall(self) -> float:
        return self.wall_value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.monotonic_value += seconds
        self.wall_value += seconds


class _FakeTransport:
    def __init__(
        self,
        clock: _FakeClock,
        outcomes: tuple[MusicBrainzTransportResponse | BaseException, ...],
    ) -> None:
        self.clock = clock
        self.outcomes = list(outcomes)
        self.requests: list = []
        self.start_times: list[float] = []

    def send(self, request):
        self.requests.append(request)
        self.start_times.append(self.clock.monotonic())
        if not self.outcomes:
            raise AssertionError("fake transport outcome queue is empty")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _FakeUrlResponse:
    def __init__(self, *, status: int, body: bytes, headers: Message) -> None:
        self.status = status
        self._body = body
        self.headers = headers

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _FakeOpener:
    def __init__(self, outcome: _FakeUrlResponse | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[tuple[object, float]] = []

    def open(self, request: object, *, timeout: float):
        self.calls.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _ClientCase(unittest.TestCase):
    def make_client(
        self,
        directory: Path,
        *outcomes: MusicBrainzTransportResponse | BaseException,
        clock: _FakeClock | None = None,
        cache_path: Path | None = None,
    ) -> tuple[ProductionMusicBrainzClient, _FakeTransport, _FakeClock]:
        fake_clock = clock or _FakeClock()
        transport = _FakeTransport(fake_clock, outcomes)
        client = ProductionMusicBrainzClient(
            user_agent=USER_AGENT,
            transport=transport,
            cache_path=cache_path or directory / "musicbrainz-v1.sqlite3",
            monotonic_clock=fake_clock.monotonic,
            wall_clock=fake_clock.wall,
            sleeper=fake_clock.sleep,
        )
        self.addCleanup(client.close)
        return client, transport, fake_clock


class UrllibMusicBrainzTransportTests(unittest.TestCase):
    def test_standard_transport_builds_one_get_without_http_types_crossing_client(
        self,
    ) -> None:
        body = b'{"recordings":[]}'
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
        opener = _FakeOpener(_FakeUrlResponse(status=200, body=body, headers=headers))
        clock = _FakeClock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            client = ProductionMusicBrainzClient(
                user_agent=USER_AGENT,
                transport=UrllibMusicBrainzTransport(opener),
                cache_path=Path(temporary_directory) / "cache.sqlite3",
                monotonic_clock=clock.monotonic,
                wall_clock=clock.wall,
                sleeper=clock.sleep,
            )
            self.addCleanup(client.close)

            self.assertEqual(
                client.search_recordings(
                    "Transport Artist",
                    "Transport Title",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                ),
                (),
            )

        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, MUSICBRAINZ_TIMEOUT_SECONDS)
        self.assertTrue(request.full_url.startswith(f"{MUSICBRAINZ_ORIGIN}/ws/2/"))
        self.assertEqual(
            request.get_header("User-agent"),
            USER_AGENT,
        )
        self.assertIsNone(request.get_header("Authorization"))

    def test_standard_transport_surfaces_redirect_without_following(self) -> None:
        headers = Message()
        headers["Location"] = "https://redirect.invalid/private"
        redirect = urllib.error.HTTPError(
            f"{MUSICBRAINZ_ORIGIN}/ws/2/recording",
            302,
            "Found",
            headers,
            BytesIO(b""),
        )
        opener = _FakeOpener(redirect)
        clock = _FakeClock()
        with tempfile.TemporaryDirectory() as temporary_directory:
            client = ProductionMusicBrainzClient(
                user_agent=USER_AGENT,
                transport=UrllibMusicBrainzTransport(opener),
                cache_path=Path(temporary_directory) / "cache.sqlite3",
                monotonic_clock=clock.monotonic,
                wall_clock=clock.wall,
                sleeper=clock.sleep,
            )
            self.addCleanup(client.close)

            with self.assertRaises(MusicBrainzRedirectError):
                client.search_recordings(
                    "Transport Artist",
                    "Redirect",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )

        self.assertEqual(len(opener.calls), 1)


class ProductionMusicBrainzRequestTests(_ClientCase):
    def test_exact_request_policy_and_release_group_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response(_fixture("release_groups.json")),
            )

            results = client.search_release_groups(
                "  Velvet ＋ Meridian/Arc ",
                'Paper:  [Constellations] "Night"?',
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

            self.assertIsInstance(client, MusicBrainzClient)
            self.assertEqual(len(transport.requests), 1)
            request = transport.requests[0]
            self.assertEqual(request.method, "GET")
            self.assertEqual(request.origin, MUSICBRAINZ_ORIGIN)
            self.assertEqual(
                request.path,
                f"{MUSICBRAINZ_API_ROOT}release-group",
            )
            self.assertEqual(request.timeout, MUSICBRAINZ_TIMEOUT_SECONDS)
            self.assertEqual(
                request.headers,
                (
                    ("Accept", "application/json"),
                    ("User-Agent", USER_AGENT),
                ),
            )
            expected_query = (
                r'releasegroup:"Paper\: \[Constellations\] \"Night\"\?" '
                r'AND artist:"Velvet \+ Meridian\/Arc"'
            )
            self.assertEqual(
                dict(request.parameters),
                {
                    "fmt": "json",
                    "limit": "10",
                    "query": expected_query,
                },
            )
            self.assertEqual(urlsplit(request.encoded_url).scheme, "https")
            self.assertEqual(
                parse_qs(urlsplit(request.encoded_url).query)["query"],
                [expected_query],
            )
            self.assertEqual(
                results,
                (
                    ReleaseGroupSearchResult(
                        mbid=UUID("11111111-1111-4111-8111-111111111111"),
                        title="Paper Constellations",
                        artist_credit="Velvet Meridian",
                        first_release_date="2031-02-03",
                        primary_type="Album",
                        secondary_types=("Compilation", "Soundtrack"),
                        search_score=92,
                    ),
                    ReleaseGroupSearchResult(
                        mbid=UUID("33333333-3333-4333-8333-333333333333"),
                        title="Quiet Geometry",
                        artist_credit="",
                        search_score=71,
                    ),
                ),
            )
            self.assertEqual(client.malformed_item_count, 3)

    def test_all_lucene_metacharacters_are_escaped_by_production_requests(
        self,
    ) -> None:
        cases = {
            "+": r"\+",
            "-": r"\-",
            "&&": r"\&&",
            "||": r"\||",
            "!": r"\!",
            "(": r"\(",
            ")": r"\)",
            "{": r"\{",
            "}": r"\}",
            "[": r"\[",
            "]": r"\]",
            "^": r"\^",
            '"': r"\"",
            "~": r"\~",
            "*": r"\*",
            "?": r"\?",
            ":": r"\:",
            "\\": r"\\",
            "/": r"\/",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                *(_response({"recordings": []}) for _ in cases),
            )
            for raw, escaped in cases.items():
                with self.subTest(raw=raw):
                    client.search_recordings(
                        "Neon Orchard",
                        f"Left{raw}Right",
                        MUSICBRAINZ_CANDIDATE_LIMIT,
                    )
                    self.assertEqual(
                        dict(transport.requests[-1].parameters)["query"],
                        f'recording:"Left{escaped}Right" AND artist:"Neon Orchard"',
                    )

    def test_recordings_are_deterministic_with_missing_optional_fields(self) -> None:
        source = _fixture("recordings.json")
        reordered = {
            **source,
            "recordings": list(reversed(source["recordings"])),
        }
        with (
            tempfile.TemporaryDirectory() as first_directory,
            tempfile.TemporaryDirectory() as second_directory,
        ):
            first, _transport, _clock = self.make_client(
                Path(first_directory),
                _response(source),
            )
            second, _transport, _clock = self.make_client(
                Path(second_directory),
                _response(reordered),
            )

            first_results = first.search_recordings(
                "Velvet Meridian",
                "Signal Garden",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            second_results = second.search_recordings(
                "Velvet Meridian",
                "Signal Garden",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

            self.assertEqual(first_results, second_results)
            self.assertEqual(
                first_results,
                (
                    RecordingSearchResult(
                        mbid=UUID("22222222-2222-4222-8222-222222222222"),
                        title="Signal Garden",
                        artist_credit="Velvet Meridian & Neon Orchard",
                        duration_ms=201250,
                        first_release_date="2031-02-03",
                        releases=(
                            (
                                UUID("66666666-6666-4666-8666-666666666666"),
                                "Paper Constellations",
                            ),
                            (
                                UUID("77777777-7777-4777-8777-777777777777"),
                                "Signal Garden Single",
                            ),
                        ),
                        search_score=97,
                    ),
                    RecordingSearchResult(
                        mbid=UUID("44444444-4444-4444-8444-444444444444"),
                        title="Lattice Rain",
                        artist_credit="",
                        search_score=70,
                    ),
                ),
            )

    def test_redirect_and_response_contract_failures_are_not_retried(self) -> None:
        cases = (
            (
                _response(
                    status=302,
                    headers=(("Location", "https://redirect.invalid/private"),),
                    body=b"",
                ),
                MusicBrainzRedirectError,
                "redirect rejected",
            ),
            (
                _response(status=400, body=b"private response body"),
                MusicBrainzRequestError,
                "HTTP status 400",
            ),
            (
                _response({"unexpected": []}),
                MusicBrainzResponseError,
                "response contract violation",
            ),
        )
        for index, (outcome, error_type, message) in enumerate(cases):
            with (
                self.subTest(error=error_type.__name__),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                client, transport, _clock = self.make_client(
                    Path(temporary_directory),
                    outcome,
                )
                with self.assertRaisesRegex(error_type, message):
                    client.search_recordings(
                        "Neon Orchard",
                        f"Glass Echoes {index}",
                        MUSICBRAINZ_CANDIDATE_LIMIT,
                    )
                self.assertEqual(len(transport.requests), 1)
                self.assertFalse(client.circuit_open)

    def test_nonempty_all_malformed_response_is_an_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response(
                    {
                        "recordings": [
                            {"title": "Missing Identifier"},
                            {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                        ]
                    }
                ),
            )

            with self.assertRaisesRegex(
                MusicBrainzResponseError,
                "no valid items",
            ):
                client.search_recordings(
                    "Neon Orchard",
                    "Glass Echoes",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )

            self.assertEqual(client.malformed_item_count, 2)
            self.assertEqual(len(transport.requests), 1)
            self.assertFalse(client.circuit_open)


class ProductionMusicBrainzCacheTests(_ClientCase):
    def test_fresh_cache_is_persistent_opaque_and_skips_live_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            cache_path = directory / "musicbrainz-v1.sqlite3"
            first, first_transport, first_clock = self.make_client(
                directory,
                _response({"recordings": []}),
                clock=_FakeClock(),
                cache_path=cache_path,
            )
            query_artist = "Invented Cache Artist"
            query_title = "Raw Query Must Stay Opaque"

            first.search_recordings(
                query_artist,
                query_title,
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            first.close()

            second, second_transport, second_clock = self.make_client(
                directory,
                clock=first_clock,
                cache_path=cache_path,
            )
            self.assertEqual(
                second.search_recordings(
                    query_artist,
                    query_title,
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                ),
                (),
            )
            self.assertEqual(len(first_transport.requests), 1)
            self.assertEqual(second_transport.requests, [])
            self.assertEqual(second_clock.sleeps, [])

            with sqlite3.connect(cache_path) as database:
                version = database.execute("PRAGMA user_version").fetchone()[0]
                columns = [
                    row[1]
                    for row in database.execute(
                        "PRAGMA table_info(responses)"
                    ).fetchall()
                ]
                (cache_key,) = database.execute(
                    "SELECT cache_key FROM responses"
                ).fetchone()
            self.assertEqual(version, MUSICBRAINZ_CACHE_SCHEMA_VERSION)
            self.assertEqual(columns, ["cache_key", "stored_at", "body"])
            self.assertRegex(cache_key, r"^[0-9a-f]{64}$")
            self.assertNotIn(query_artist, cache_key)
            self.assertNotIn(query_title, cache_key)
            self.assertNotIn(
                query_title.encode(),
                cache_path.read_bytes(),
            )

    def test_expired_entry_is_a_miss_and_successfully_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            clock = _FakeClock()
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response({"recordings": []}),
                _response(
                    {
                        "recordings": [
                            {
                                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                                "title": "Renewed Result",
                                "score": 90,
                            }
                        ]
                    }
                ),
                clock=clock,
            )
            call = (
                "Cache Artist",
                "Expiring Query",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

            self.assertEqual(client.search_recordings(*call), ())
            clock.advance(MUSICBRAINZ_CACHE_MAX_AGE_SECONDS + 1)
            renewed = client.search_recordings(*call)

            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(renewed[0].title, "Renewed Result")

    def test_corrupt_entry_is_deleted_and_refreshed_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            cache_path = directory / "musicbrainz-v1.sqlite3"
            clock = _FakeClock()
            client, transport, _clock = self.make_client(
                directory,
                _response({"release-groups": []}),
                _response({"release-groups": []}),
                clock=clock,
                cache_path=cache_path,
            )
            call = (
                "Cache Artist",
                "Corrupt Entry",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            client.search_release_groups(*call)
            with sqlite3.connect(cache_path) as database:
                database.execute("UPDATE responses SET body = ?", (b"{",))
                database.commit()
            clock.advance(MUSICBRAINZ_RATE_INTERVAL_SECONDS)

            self.assertEqual(client.search_release_groups(*call), ())
            self.assertEqual(len(transport.requests), 2)

    def test_expired_data_is_not_used_when_live_refresh_exhausts_retries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            clock = _FakeClock()
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response(
                    {
                        "recordings": [
                            {
                                "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                                "title": "Stale Result",
                                "score": 90,
                            }
                        ]
                    }
                ),
                *(
                    _response(status=503, body=b"unavailable")
                    for _ in range(MUSICBRAINZ_MAX_ATTEMPTS)
                ),
                clock=clock,
            )
            call = (
                "Cache Artist",
                "No Stale Fallback",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            self.assertEqual(client.search_recordings(*call)[0].title, "Stale Result")
            clock.advance(MUSICBRAINZ_CACHE_MAX_AGE_SECONDS + 1)

            with self.assertRaises(MusicBrainzRetryExhausted):
                client.search_recordings(*call)

            self.assertEqual(len(transport.requests), 5)

    def test_only_complete_valid_200_responses_are_cached(self) -> None:
        retryable_cases = (
            _response({"recordings": []}, complete=False),
            _response(body=b"{"),
            _response(status=503, body=b"service unavailable"),
        )
        for label, retryable in (
            ("partial", retryable_cases[0]),
            ("malformed JSON", retryable_cases[1]),
            ("HTTP error", retryable_cases[2]),
        ):
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                directory = Path(temporary_directory)
                cache_path = directory / "musicbrainz-v1.sqlite3"
                client, transport, _clock = self.make_client(
                    directory,
                    *(retryable for _ in range(MUSICBRAINZ_MAX_ATTEMPTS)),
                    cache_path=cache_path,
                )

                with self.assertRaises(MusicBrainzRetryExhausted):
                    client.search_recordings(
                        "No Cache Artist",
                        label,
                        MUSICBRAINZ_CANDIDATE_LIMIT,
                    )

                with sqlite3.connect(cache_path) as database:
                    count = database.execute(
                        "SELECT COUNT(*) FROM responses"
                    ).fetchone()[0]
                self.assertEqual(len(transport.requests), MUSICBRAINZ_MAX_ATTEMPTS)
                self.assertEqual(count, 0)

    def test_cache_file_and_directory_use_private_permissions(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX permission bits are unavailable on Windows")
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "private" / "cache.sqlite3"
            cache = MusicBrainzCache(cache_path)
            self.addCleanup(cache.close)

            self.assertEqual(
                stat.S_IMODE(cache_path.parent.stat().st_mode),
                0o700,
            )
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)

    def test_default_cache_path_is_versioned_and_per_user(self) -> None:
        path = default_musicbrainz_cache_path()
        self.assertEqual(path.name, "musicbrainz-v1.sqlite3")
        self.assertEqual(path.parent.name, "music-manager")
        self.assertTrue(path.is_absolute())


class ProductionMusicBrainzRetryTests(_ClientCase):
    def test_rate_gate_and_backoff_are_deterministic_across_all_failure_types(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, clock = self.make_client(
                Path(temporary_directory),
                MusicBrainzTransportError("private transport details"),
                _response(status=503, body=b"private response"),
                _response(body=b"{"),
                _response({"recordings": []}),
            )

            self.assertEqual(
                client.search_recordings(
                    "Retry Artist",
                    "Retry Title",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                ),
                (),
            )

            self.assertEqual(transport.start_times, [0.0, 1.1, 3.1, 7.1])
            self.assertEqual(clock.sleeps, [1.1, 2.0, 4.0])

    def test_timeouts_are_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, clock = self.make_client(
                Path(temporary_directory),
                TimeoutError("private timeout details"),
                _response({"recordings": []}),
            )

            self.assertEqual(
                client.search_recordings(
                    "Timeout Artist",
                    "Timeout Title",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                ),
                (),
            )
            self.assertEqual(transport.start_times, [0.0, 1.1])
            self.assertEqual(clock.sleeps, [1.1])

    def test_every_retryable_http_status_is_retried(self) -> None:
        for status in (429, 500, 502, 503, 504):
            with (
                self.subTest(status=status),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                client, transport, _clock = self.make_client(
                    Path(temporary_directory),
                    _response(status=status, body=b"private response"),
                    _response({"recordings": []}),
                )

                self.assertEqual(
                    client.search_recordings(
                        "HTTP Artist",
                        f"Status {status}",
                        MUSICBRAINZ_CANDIDATE_LIMIT,
                    ),
                    (),
                )
                self.assertEqual(len(transport.requests), 2)

    def test_successive_live_operations_share_the_rate_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, clock = self.make_client(
                Path(temporary_directory),
                _response({"recordings": []}),
                _response({"recordings": []}),
            )

            client.search_recordings(
                "Rate Artist",
                "First",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            client.search_recordings(
                "Rate Artist",
                "Second",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

            self.assertEqual(transport.start_times, [0.0, 1.1])
            self.assertEqual(clock.sleeps, [1.1])

    def test_fresh_cache_hit_consumes_no_sleep_or_rate_limit_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, clock = self.make_client(
                Path(temporary_directory),
                _response({"recordings": []}),
                _response({"recordings": []}),
            )
            cached_call = (
                "Rate Artist",
                "Cached",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

            client.search_recordings(*cached_call)
            client.search_recordings(*cached_call)
            client.search_recordings(
                "Rate Artist",
                "Uncached",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(transport.start_times, [0.0, 1.1])
            self.assertEqual(clock.sleeps, [1.1])

    def test_retry_after_delta_and_http_date_control_the_retry_delay(self) -> None:
        for label, header_value, expected_delay in (
            ("delta", "3", 3.0),
            (
                "date",
                format_datetime(
                    datetime.fromtimestamp(
                        1_800_000_005,
                        tz=timezone.utc,
                    ),
                    usegmt=True,
                ),
                5.0,
            ),
        ):
            with (
                self.subTest(label=label),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                client, transport, clock = self.make_client(
                    Path(temporary_directory),
                    _response(
                        status=503,
                        headers=(("Retry-After", header_value),),
                        body=b"",
                    ),
                    _response({"release-groups": []}),
                )

                self.assertEqual(
                    client.search_release_groups(
                        "Retry Artist",
                        label,
                        MUSICBRAINZ_CANDIDATE_LIMIT,
                    ),
                    (),
                )
                self.assertEqual(transport.start_times, [0.0, expected_delay])
                self.assertEqual(clock.sleeps, [expected_delay])

    def test_retry_after_over_sixty_stops_immediately_and_opens_circuit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response(
                    status=503,
                    headers=(("Retry-After", "61"),),
                    body=b"",
                ),
            )

            with self.assertRaises(MusicBrainzRetryExhausted):
                client.search_recordings(
                    "Retry Artist",
                    "Long Delay",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )

            self.assertEqual(len(transport.requests), 1)
            self.assertTrue(client.circuit_open)

    def test_retry_exhaustion_opens_run_circuit_but_cache_hits_still_work(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response({"recordings": []}),
                *(
                    _response(status=503, body=b"unavailable")
                    for _ in range(MUSICBRAINZ_MAX_ATTEMPTS)
                ),
            )
            cached_call = (
                "Circuit Artist",
                "Cached",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            client.search_recordings(*cached_call)

            with self.assertRaises(MusicBrainzRetryExhausted):
                client.search_recordings(
                    "Circuit Artist",
                    "Fails",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )

            self.assertTrue(client.circuit_open)
            self.assertEqual(client.search_recordings(*cached_call), ())
            with self.assertRaises(MusicBrainzCircuitOpen):
                client.search_recordings(
                    "Circuit Artist",
                    "Blocked",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )
            self.assertEqual(len(transport.requests), 5)


class ProductionMusicBrainzPrivacyTests(_ClientCase):
    def test_injected_client_needs_no_dns_socket_or_real_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            client, transport, _clock = self.make_client(
                Path(temporary_directory),
                _response({"recordings": []}),
            )
            with (
                mock.patch.object(
                    socket,
                    "getaddrinfo",
                    side_effect=AssertionError("DNS accessed"),
                ),
                mock.patch.object(
                    socket,
                    "create_connection",
                    side_effect=AssertionError("socket accessed"),
                ),
                mock.patch.object(
                    time,
                    "sleep",
                    side_effect=AssertionError("real sleep accessed"),
                ),
            ):
                self.assertEqual(
                    client.search_recordings(
                        "Offline Artist",
                        "Offline Title",
                        MUSICBRAINZ_CANDIDATE_LIMIT,
                    ),
                    (),
                )
            self.assertEqual(len(transport.requests), 1)

    def test_private_values_never_enter_requests_cache_keys_or_errors(self) -> None:
        private_values = {
            "path": "/private-home/library/01-secret.flac",
            "filename": "01-secret.flac",
            "scan_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "file_record_id": "bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb",
            "album_group_id": "cccccccc-cccc-5ccc-8ccc-cccccccccccc",
            "fingerprint": "stat-v1:private-fingerprint",
            "checksum": "private-checksum",
            "config_path": "/private-home/config/music-manager.yml",
            "username": "private-user",
            "hostname": "private-host",
            "audio": "private-audio-bytes",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            cache_path = directory / "musicbrainz-v1.sqlite3"
            client, transport, _clock = self.make_client(
                directory,
                *(
                    MusicBrainzTransportError(
                        f"transport leaked {private_values['path']}"
                    )
                    for _ in range(MUSICBRAINZ_MAX_ATTEMPTS)
                ),
                cache_path=cache_path,
            )

            with self.assertRaises(MusicBrainzRetryExhausted) as raised:
                client.search_recordings(
                    "Allowlisted Artist",
                    "Allowlisted Title",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )

            request_boundary = repr(transport.requests)
            error_boundary = repr(raised.exception)
            cache_keys = [
                build_musicbrainz_cache_key(request) for request in transport.requests
            ]
            for allowed in ("Allowlisted Artist", "Allowlisted Title"):
                self.assertIn(allowed, request_boundary)
            for label, private in private_values.items():
                with self.subTest(label=label):
                    self.assertNotIn(private, request_boundary)
                    self.assertNotIn(private, error_boundary)
                    self.assertTrue(
                        all(private not in key for key in cache_keys),
                    )
            self.assertNotIn(
                private_values["path"].encode(),
                cache_path.read_bytes(),
            )

    def test_invalid_user_agent_fails_before_cache_or_transport_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_path = Path(temporary_directory) / "must-not-exist.sqlite3"
            for invalid in (
                "",
                "music-manager/0.4.0 (private@example.invalid)",
                "music-manager/0.4.0 local (https://github.com/saari-co/music-manager)",
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    ProductionMusicBrainzClient(
                        user_agent=invalid,
                        transport=mock.Mock(),
                        cache_path=cache_path,
                    )
            self.assertFalse(cache_path.exists())


if __name__ == "__main__":
    unittest.main()
