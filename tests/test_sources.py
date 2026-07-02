"""Tests for library-source detection and aggregation."""

from __future__ import annotations

import unittest
from pathlib import Path

from music_manager.models import ScanRecord
from music_manager.sources import (
    consolidate_library_sources,
    source_name_for_relative_path,
)


class LibrarySourceTests(unittest.TestCase):
    def test_detects_apple_music_media_layout(self) -> None:
        source_name = source_name_for_relative_path(
            Path("Music/Media.localized/Music/Artist/Album/Track.m4a")
        )

        self.assertEqual(source_name, "Apple Music")

    def test_large_library_aggregates_small_top_level_groups(self) -> None:
        records = [
            self._record(f"Major/Album/{index}.mp3", "Major")
            for index in range(250)
        ]
        records.extend(
            self._record(
                f"Artist {index}/Album/Track.mp3",
                f"Artist {index}",
            )
            for index in range(750)
        )

        source_counts = consolidate_library_sources(records)

        self.assertEqual(
            source_counts,
            {"Major": 250, "Root Library": 750},
        )

    @staticmethod
    def _record(path: str, library_source: str) -> ScanRecord:
        return ScanRecord(
            path=Path(path),
            extension=".mp3",
            file_type="audio",
            library_source=library_source,
        )


if __name__ == "__main__":
    unittest.main()
