"""Focused identity, fingerprint, and matching contract regressions."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from music_manager.analyzer import analyze_library
from music_manager.scanner import scan_library


FIRST_SCAN_ID = UUID("12345678-1234-4abc-8def-1234567890ab")
SECOND_SCAN_ID = UUID("87654321-4321-4abc-8def-1234567890ab")


class _FakeInfo:
    bitrate = 192000
    length = 180.0
    sample_rate = 44100
    bits_per_sample = 16
    channels = 2


class _FakeAudio:
    def __init__(self, title: str) -> None:
        self.tags = {
            "artist": ["Synthetic Artist"],
            "title": [title],
            "album": ["Synthetic Album"],
            "date": ["2026"],
            "tracknumber": ["1"],
        }
        self.info = _FakeInfo()


class IdentityAndMatchingContractTests(unittest.TestCase):
    """Keep scan-local identity and stat hints separate from matching."""

    def test_ids_fingerprints_duplicates_and_content_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory).resolve()
            paths = {
                "A.mp3": source / "A.mp3",
                "B.mp3": source / "B.mp3",
                "C.mp3": source / "C.mp3",
            }
            paths["A.mp3"].write_bytes(b"AAAA")
            paths["B.mp3"].write_bytes(b"BBBBBBBB")
            paths["C.mp3"].write_bytes(b"CCCC")
            fixed_mtime = 1_700_000_000_123_456_789
            os.utime(paths["A.mp3"], ns=(fixed_mtime, fixed_mtime))
            os.utime(paths["B.mp3"], ns=(fixed_mtime + 1, fixed_mtime + 1))
            os.utime(paths["C.mp3"], ns=(fixed_mtime, fixed_mtime))
            original_bytes = {name: path.read_bytes() for name, path in paths.items()}

            def metadata(path: Path) -> _FakeAudio:
                title = "Same Song" if path.name in {"A.mp3", "B.mp3"} else "Other"
                return _FakeAudio(title)

            first_scan = scan_library(
                source,
                metadata_loader=metadata,
                scan_id=FIRST_SCAN_ID,
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()},
                original_bytes,
            )
            first_by_path = {
                record.relative_path: record for record in first_scan.records
            }

            self.assertEqual(
                len({record.file_record_id for record in first_scan.records}),
                3,
            )
            self.assertNotEqual(
                first_by_path["A.mp3"].file_fingerprint,
                first_by_path["B.mp3"].file_fingerprint,
            )
            self.assertEqual(
                first_by_path["A.mp3"].file_fingerprint,
                first_by_path["C.mp3"].file_fingerprint,
            )

            analysis = analyze_library(first_scan.records)
            self.assertEqual(len(analysis.duplicate_groups), 1)
            self.assertEqual(
                {
                    record.relative_path
                    for record in analysis.duplicate_groups[0].records
                },
                {"A.mp3", "B.mp3"},
            )

            second_scan = scan_library(
                source,
                metadata_loader=metadata,
                scan_id=SECOND_SCAN_ID,
            )
            second_by_path = {
                record.relative_path: record for record in second_scan.records
            }
            for relative_path in paths:
                self.assertNotEqual(
                    first_by_path[relative_path].file_record_id,
                    second_by_path[relative_path].file_record_id,
                )
                self.assertEqual(
                    first_by_path[relative_path].file_fingerprint,
                    second_by_path[relative_path].file_fingerprint,
                )

            paths["A.mp3"].write_bytes(b"ZZZZ")
            os.utime(paths["A.mp3"], ns=(fixed_mtime, fixed_mtime))
            changed_bytes = {name: path.read_bytes() for name, path in paths.items()}
            changed_content = scan_library(
                source,
                metadata_loader=metadata,
                scan_id=FIRST_SCAN_ID,
            )
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()},
                changed_bytes,
            )
            changed_a = next(
                record
                for record in changed_content.records
                if record.relative_path == "A.mp3"
            )
            self.assertEqual(
                changed_a.file_record_id, first_by_path["A.mp3"].file_record_id
            )
            self.assertEqual(
                changed_a.file_fingerprint,
                first_by_path["A.mp3"].file_fingerprint,
            )
            self.assertNotEqual(changed_bytes["A.mp3"], original_bytes["A.mp3"])


if __name__ == "__main__":
    unittest.main()
