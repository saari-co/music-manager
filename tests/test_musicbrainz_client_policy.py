"""Deterministic MusicBrainz client-policy tests using injected fakes only."""

from __future__ import annotations

import json
import socket
import time
import unittest
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
from tests.musicbrainz_fakes import (
    MUSICBRAINZ_API_ROOT,
    MUSICBRAINZ_ORIGIN,
    FakeMusicBrainzClient,
    FakeResponse,
    FakeTransport,
    MalformedFakeResponse,
    RedirectRejected,
)


FIXTURES = Path(__file__).parent / "fixtures" / "musicbrainz"
USER_AGENT = "music-manager/0.4.0 (https://github.com/saari-co/music-manager)"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client(*payloads: object) -> tuple[FakeMusicBrainzClient, FakeTransport]:
    transport = FakeTransport(
        tuple(FakeResponse(status=200, payload=payload) for payload in payloads)
    )
    return (
        FakeMusicBrainzClient(user_agent=USER_AGENT, transport=transport),
        transport,
    )


class MusicBrainzRequestPolicyTests(unittest.TestCase):
    def test_fake_client_implements_application_owned_boundary(self) -> None:
        client, _transport = _client()
        self.assertIsInstance(client, MusicBrainzClient)

    def test_release_group_request_has_exact_identity_origin_and_json_policy(
        self,
    ) -> None:
        client, transport = _client({"release-groups": []})

        self.assertEqual(build_musicbrainz_user_agent("0.4.0"), USER_AGENT)
        client.search_release_groups(
            "Velvet Meridian",
            "Paper Constellations",
            MUSICBRAINZ_CANDIDATE_LIMIT,
        )

        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.origin, MUSICBRAINZ_ORIGIN)
        self.assertEqual(request.path, f"{MUSICBRAINZ_API_ROOT}release-group")
        self.assertEqual(
            request.headers,
            (
                ("Accept", "application/json"),
                ("User-Agent", USER_AGENT),
            ),
        )
        self.assertEqual(
            dict(request.parameters),
            {
                "fmt": "json",
                "limit": "10",
                "query": (
                    'releasegroup:"Paper Constellations" AND artist:"Velvet Meridian"'
                ),
            },
        )
        self.assertEqual(urlsplit(request.encoded_url).scheme, "https")

    def test_recording_query_normalizes_then_escapes_before_url_encoding(
        self,
    ) -> None:
        client, transport = _client({"recordings": []})

        client.search_recordings(
            "  Neon ＋ Orchard/Arc  ",
            'Glass:  [Echoes] "Night"?',
            MUSICBRAINZ_CANDIDATE_LIMIT,
        )

        request = transport.requests[0]
        expected = (
            r'recording:"Glass\: \[Echoes\] \"Night\"\?" '
            r'AND artist:"Neon \+ Orchard\/Arc"'
        )
        self.assertEqual(dict(request.parameters)["query"], expected)
        self.assertEqual(
            parse_qs(urlsplit(request.encoded_url).query)["query"],
            [expected],
        )
        self.assertNotIn("[Echoes]", request.encoded_url)
        self.assertIn("%5C%5B", request.encoded_url)

    def test_all_lucene_metacharacters_are_escaped(self) -> None:
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
        for raw, escaped in cases.items():
            with self.subTest(raw=raw):
                client, transport = _client({"recordings": []})
                client.search_recordings(
                    "Neon Orchard",
                    f"Left{raw}Right",
                    MUSICBRAINZ_CANDIDATE_LIMIT,
                )
                self.assertEqual(
                    dict(transport.requests[0].parameters)["query"],
                    f'recording:"Left{escaped}Right" AND artist:"Neon Orchard"',
                )

    def test_fake_transport_rejects_redirect_without_following_it(self) -> None:
        transport = FakeTransport(
            (
                FakeResponse(
                    status=302,
                    headers=(("Location", "https://redirect.invalid/private"),),
                ),
                FakeResponse(status=200, payload={"recordings": []}),
            )
        )
        client = FakeMusicBrainzClient(
            user_agent=USER_AGENT,
            transport=transport,
        )

        with self.assertRaisesRegex(RedirectRejected, "redirect rejected"):
            client.search_recordings(
                "Neon Orchard",
                "Glass Echoes",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

        self.assertEqual(len(transport.requests), 1)

    def test_fakes_need_no_dns_socket_or_wall_clock_sleep(self) -> None:
        client, transport = _client(
            {"release-groups": []},
            {"recordings": []},
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
                side_effect=AssertionError("wall-clock sleep accessed"),
            ),
        ):
            client.search_release_groups(
                "Velvet Meridian",
                "Paper Constellations",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
            client.search_recordings(
                "Neon Orchard",
                "Glass Echoes",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )

        self.assertEqual(len(transport.requests), 2)

    def test_only_allowlisted_text_reaches_client_and_transport_calls(self) -> None:
        local_subject = {
            "artist": "Velvet Meridian",
            "album": "Paper Constellations",
            "title": "Signal Garden",
            "path": "/private-home/hidden-library/01-secret.flac",
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
        client, transport = _client(
            {"release-groups": []},
            {"recordings": []},
        )

        client.search_release_groups(
            local_subject["artist"],
            local_subject["album"],
            MUSICBRAINZ_CANDIDATE_LIMIT,
        )
        client.search_recordings(
            local_subject["artist"],
            local_subject["title"],
            MUSICBRAINZ_CANDIDATE_LIMIT,
        )

        client_boundary = repr(client.search_calls)
        transport_boundary = repr(transport.requests)
        for field in ("artist", "album", "title"):
            self.assertIn(local_subject[field], client_boundary)
            self.assertIn(local_subject[field], transport_boundary)
        for field in (
            "path",
            "filename",
            "scan_id",
            "file_record_id",
            "album_group_id",
            "fingerprint",
            "checksum",
            "config_path",
            "username",
            "hostname",
            "audio",
        ):
            with self.subTest(field=field):
                self.assertNotIn(local_subject[field], client_boundary)
                self.assertNotIn(local_subject[field], transport_boundary)


class MusicBrainzResponsePolicyTests(unittest.TestCase):
    def test_release_group_fixture_normalizes_deduplicates_and_ignores_malformed(
        self,
    ) -> None:
        client, _transport = _client(_fixture("release_groups.json"))

        results = client.search_release_groups(
            "Velvet Meridian",
            "Paper Constellations",
            MUSICBRAINZ_CANDIDATE_LIMIT,
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

    def test_recording_fixture_normalizes_deduplicates_and_missing_optional_fields(
        self,
    ) -> None:
        client, _transport = _client(_fixture("recordings.json"))

        results = client.search_recordings(
            "Velvet Meridian",
            "Signal Garden",
            MUSICBRAINZ_CANDIDATE_LIMIT,
        )

        self.assertEqual(
            results,
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
        self.assertEqual(client.malformed_item_count, 3)

    def test_reordered_response_items_produce_identical_boundary_values(self) -> None:
        release_groups = _fixture("release_groups.json")
        recordings = _fixture("recordings.json")
        reordered_release_groups = {
            **release_groups,
            "release-groups": list(reversed(release_groups["release-groups"])),
        }
        reordered_recordings = {
            **recordings,
            "recordings": list(reversed(recordings["recordings"])),
        }
        first, _transport = _client(release_groups, recordings)
        second, _transport = _client(
            reordered_release_groups,
            reordered_recordings,
        )

        first_values = (
            first.search_release_groups(
                "Velvet Meridian",
                "Paper Constellations",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            ),
            first.search_recordings(
                "Velvet Meridian",
                "Signal Garden",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            ),
        )
        second_values = (
            second.search_release_groups(
                "Velvet Meridian",
                "Paper Constellations",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            ),
            second.search_recordings(
                "Velvet Meridian",
                "Signal Garden",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            ),
        )

        self.assertEqual(first_values, second_values)
        for values in first_values:
            self.assertTrue(
                all(
                    isinstance(
                        value,
                        ReleaseGroupSearchResult | RecordingSearchResult,
                    )
                    for value in values
                )
            )

    def test_missing_required_fields_and_malformed_envelopes_are_explicit(
        self,
    ) -> None:
        client, _transport = _client(
            {
                "release-groups": [
                    {"id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                    {"title": "Missing Identifier"},
                ]
            },
            [],
            {"unexpected": []},
        )

        self.assertEqual(
            client.search_release_groups(
                "Velvet Meridian",
                "Paper Constellations",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            ),
            (),
        )
        self.assertEqual(client.malformed_item_count, 2)
        with self.assertRaisesRegex(
            MalformedFakeResponse,
            "response body must be an object",
        ):
            client.search_recordings(
                "Velvet Meridian",
                "Signal Garden",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )
        with self.assertRaisesRegex(
            MalformedFakeResponse,
            "'recordings' must be a list",
        ):
            client.search_recordings(
                "Velvet Meridian",
                "Signal Garden",
                MUSICBRAINZ_CANDIDATE_LIMIT,
            )


if __name__ == "__main__":
    unittest.main()
