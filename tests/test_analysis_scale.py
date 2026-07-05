"""Deterministic scale coverage for the in-memory analysis boundary."""

from __future__ import annotations

import unittest
from unittest import mock

from music_manager.analyzer import analyze_library, normalize_metadata


ROW_COUNT = 100_000


class _ScaleRecord:
    """A small synthetic row with counters for duplicate-match work."""

    __slots__ = ("_duration_reads", "path")

    file_type = "audio"
    status = "ok"
    artist = "Synthetic Scale Artist"
    title = "Synthetic Scale Track"
    album = "Synthetic Scale Album"
    date_year = "2026"
    track_number = "1"
    extension = ".mp3"
    bitrate_kbps = 192.0

    def __init__(self, index: int, duration_reads: list[int]) -> None:
        self.path = f"generated/{index:06d}.mp3"
        self._duration_reads = duration_reads

    @property
    def duration_seconds(self) -> float:
        self._duration_reads[0] += 1
        return 180.0


class AnalysisScaleTests(unittest.TestCase):
    """Catch count regressions and pairwise duplicate comparisons."""

    def test_one_hundred_thousand_rows_have_linear_match_inputs(self) -> None:
        duration_reads = [0]
        records = [_ScaleRecord(index, duration_reads) for index in range(ROW_COUNT)]

        with mock.patch(
            "music_manager.analyzer.normalize_metadata",
            wraps=normalize_metadata,
        ) as normalize:
            analysis = analyze_library(records)

        self.assertEqual(analysis.summary.root_library_total, ROW_COUNT)
        self.assertEqual(analysis.summary.duplicate_candidate_groups, 1)
        self.assertEqual(analysis.summary.duplicate_candidate_files, ROW_COUNT)
        self.assertEqual(len(analysis.duplicate_groups[0].records), ROW_COUNT)
        self.assertEqual(analysis.summary.files_with_missing_metadata, 0)
        self.assertEqual(analysis.summary.corrupt_or_unreadable_files, 0)
        self.assertEqual(analysis.quality_buckets["192_to_255"], ROW_COUNT)
        self.assertEqual(normalize.call_count, ROW_COUNT * 2)
        self.assertEqual(duration_reads[0], ROW_COUNT * 3)


if __name__ == "__main__":
    unittest.main()
