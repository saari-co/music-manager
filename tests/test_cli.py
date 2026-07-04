"""Regression tests for the installed CLI and its safety boundaries."""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional
from unittest import mock

from music_manager.cli import main
from music_manager.models import ScanRecord
from music_manager.reports import (
    ANALYSIS_REPORT_FILENAMES,
    write_csv_report,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _FakeInfo:
    bitrate = 192000
    length = 180.0


class _FakeAudio:
    tags = {
        "artist": ["Test Artist"],
        "title": ["Test Title"],
        "album": ["Test Album"],
        "date": ["2026"],
        "tracknumber": ["1"],
    }
    info = _FakeInfo()


def _source_snapshot(source: Path) -> dict[str, Optional[bytes]]:
    """Capture every source path and file payload."""
    return {
        path.relative_to(source).as_posix(): (
            None if path.is_dir() else path.read_bytes()
        )
        for path in sorted(source.rglob("*"))
    }


class CliRegressionTests(unittest.TestCase):
    """Characterize the released command-line behavior."""

    def test_installed_and_module_help_entry_points_match(self) -> None:
        scripts_directory = Path(sysconfig.get_path("scripts"))
        command_name = (
            "music-manager.exe" if sys.platform == "win32" else "music-manager"
        )
        console_command = scripts_directory / command_name
        self.assertTrue(
            console_command.is_file(),
            f"console command is not installed: {console_command}",
        )

        outputs = []
        for command in (
            [str(console_command), "--help"],
            [sys.executable, "-m", "music_manager.cli", "--help"],
        ):
            with self.subTest(command=command):
                result = subprocess.run(
                    command,
                    cwd=PROJECT_ROOT,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertIn("usage: music-manager", result.stdout)
                self.assertIn("{scan,analyze}", result.stdout)
                self.assertIn(
                    "Music files are never renamed, moved, copied, deleted, "
                    "or edited.",
                    result.stdout,
                )
                outputs.append(result.stdout)

        self.assertEqual(outputs[0], outputs[1])

    def test_scan_cli_applies_config_without_changing_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            ignored = source / "Ignored"
            ignored.mkdir(parents=True)
            audio_path = source / "Keep.mp3"
            archive_path = source / "Archive.zip"
            ignored_audio_path = ignored / "Skip.mp3"
            audio_path.write_bytes(b"synthetic audio payload")
            archive_path.write_bytes(b"not opened as a ZIP archive")
            ignored_audio_path.write_bytes(b"ignored audio payload")
            source_before = _source_snapshot(source)

            config_path = root / "music-manager.yml"
            config_path.write_text(
                "path_mode: absolute\nignore:\n  - Ignored\n",
                encoding="utf-8",
            )
            report_path = root / "reports" / "library_scan.csv"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_SCAN_REPORT_PATH",
                    report_path,
                ),
                mock.patch(
                    "music_manager.cli.metadata_reader_available",
                    return_value=True,
                ),
                mock.patch(
                    "music_manager.scanner._load_metadata",
                    return_value=_FakeAudio(),
                ) as metadata_loader,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "scan",
                        "--source",
                        str(source),
                        "--config",
                        str(config_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertEqual(_source_snapshot(source), source_before)
            metadata_loader.assert_called_once_with(audio_path)

            with report_path.open(encoding="utf-8", newline="") as report:
                rows = list(csv.DictReader(report))

            self.assertEqual(
                {row["path"] for row in rows},
                {str(audio_path), str(archive_path)},
            )
            self.assertNotIn(str(ignored_audio_path), report_path.read_text())
            self.assertIn("Root Library total: 1", stdout.getvalue())
            self.assertIn("Archives: 1", stdout.getvalue())

    def test_cli_rejects_unknown_config_before_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            source.mkdir()
            config_path = root / "music-manager.yml"
            config_path.write_text(
                "path_mode: relative\nunknown_setting: true\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()

            with (
                mock.patch("music_manager.cli.scan_library") as scan_library,
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "scan",
                        "--source",
                        str(source),
                        "--config",
                        str(config_path),
                    ]
                )

            self.assertEqual(exit_code, 2)
            scan_library.assert_not_called()
            self.assertIn(
                "unknown configuration key: unknown_setting",
                stderr.getvalue(),
            )

    def test_analysis_cli_does_not_require_referenced_music_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            missing_audio_path = root / "missing" / "Track.mp3"
            scan_report = root / "library_scan.csv"
            write_csv_report(
                [
                    ScanRecord(
                        path=missing_audio_path,
                        extension=".mp3",
                        file_type="audio",
                        artist="Test Artist",
                        title="Test Title",
                        album="Test Album",
                        date_year="2026",
                        track_number="1",
                        bitrate_kbps=192,
                        duration_seconds=180,
                    )
                ],
                scan_report,
                path_mode="absolute",
            )
            reports_directory = root / "analysis"
            stdout = io.StringIO()

            self.assertFalse(missing_audio_path.exists())
            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports_directory,
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "analyze",
                        "--scan-report",
                        str(scan_report),
                        "--path-mode",
                        "absolute",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertFalse(missing_audio_path.exists())
            self.assertIn("Analysis complete", stdout.getvalue())
            for filename in ANALYSIS_REPORT_FILENAMES.values():
                self.assertTrue((reports_directory / filename).is_file())


if __name__ == "__main__":
    unittest.main()
