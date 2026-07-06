"""Regression tests for the installed CLI and its safety boundaries."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional
from unittest import mock

from music_manager.artifact_schema import validate_artifact_set
from music_manager.cli import main
from music_manager.musicbrainz_client import MusicBrainzRequestError
from music_manager.models import ScanRecord
from music_manager.reports import (
    ANALYSIS_REPORT_FILENAMES,
    write_csv_report,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _EmptyMusicBrainzClient:
    def __init__(self, *, album_error: Exception | None = None) -> None:
        self.album_error = album_error
        self.calls: list[str] = []
        self.closed = False
        self.malformed_item_count = 0

    def search_release_groups(
        self,
        album_artist: str,
        album_title: str,
        limit: int,
    ) -> tuple:
        self.calls.append("album")
        if self.album_error is not None:
            raise self.album_error
        return ()

    def search_recordings(
        self,
        track_artist: str,
        track_title: str,
        limit: int,
    ) -> tuple:
        self.calls.append("recording")
        return ()

    def close(self) -> None:
        self.closed = True


def _source_snapshot(source: Path) -> dict[str, Optional[bytes]]:
    """Capture every source path and file payload."""
    return {
        path.relative_to(source).as_posix(): (
            None if path.is_dir() else path.read_bytes()
        )
        for path in sorted(source.rglob("*"))
    }


def _write_synthetic_wav(path: Path) -> None:
    """Write a small valid audio fixture without external media."""
    with wave.open(str(path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(8000)
        audio_file.writeframes(b"\x00\x00" * 800)


class CliRegressionTests(unittest.TestCase):
    """Characterize the released command-line behavior."""

    def test_installed_and_module_help_entry_points_work(self) -> None:
        console_command = shutil.which("music-manager")
        if console_command is None:
            self.fail("music-manager console command is not installed")

        for command in (
            [console_command, "--help"],
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
                self.assertIn("{scan,analyze,match}", result.stdout)
                self.assertIn(
                    "Music files are never renamed, moved, copied, deleted, or edited.",
                    result.stdout,
                )

    def test_scan_cli_applies_config_without_changing_source_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            ignored = source / "Ignored"
            ignored.mkdir(parents=True)
            audio_path = source / "Keep.wav"
            archive_path = source / "Archive.zip"
            ignored_audio_path = ignored / "Skip.wav"
            _write_synthetic_wav(audio_path)
            archive_path.write_bytes(b"not opened as a ZIP archive")
            _write_synthetic_wav(ignored_audio_path)
            source_before = _source_snapshot(source)

            config_path = root / "music-manager.yml"
            config_path.write_text(
                "path_mode: relative\nignore:\n  - Ignored\n",
                encoding="utf-8",
            )
            reports = root / "reports"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports,
                ),
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

            run_directories = [path for path in reports.iterdir() if path.is_dir()]
            self.assertEqual(len(run_directories), 1)
            run_directory = run_directories[0]
            artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(
                {row.path for row in artifacts.library_rows},
                {"Keep.wav", "Archive.zip"},
            )
            self.assertEqual(artifacts.manifest.state, "complete")
            self.assertNotIn(
                str(ignored_audio_path),
                (run_directory / "library_scan.csv").read_text(),
            )
            self.assertNotIn(
                str(root),
                (run_directory / "scan_manifest.json").read_text(),
            )
            self.assertIn("Root Library total: 1", stdout.getvalue())
            self.assertIn("Archives: 1", stdout.getvalue())
            self.assertIn(f"Reports directory: {run_directory}", stdout.getvalue())
            self.assertIn("Scan state: complete", stdout.getvalue())

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
            reports = root / "reports"

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports,
                ),
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
            self.assertFalse(reports.exists())
            self.assertIn(
                "unknown configuration key: unknown_setting",
                stderr.getvalue(),
            )

    def test_scan_cli_rejects_absolute_path_mode_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            reports = root / "reports"
            source.mkdir()
            stderr = io.StringIO()

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports,
                ),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "scan",
                        "--source",
                        str(source),
                        "--path-mode",
                        "absolute",
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertFalse(reports.exists())
            self.assertIn(
                "schema 1 rejects absolute path output",
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

    def test_analysis_cli_accepts_explicit_versioned_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scan_id = "12345678-1234-4abc-8def-1234567890ab"
            run_directory = root / "reports" / scan_id
            shutil.copytree(
                PROJECT_ROOT / "tests" / "fixtures" / "v0_3" / "valid",
                run_directory,
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "analyze",
                        "--scan-run",
                        str(run_directory),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn(f"Reports directory: {run_directory}", stdout.getvalue())
            artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(
                {
                    entry.filename
                    for entry in artifacts.manifest.artifacts.values()
                    if entry.role == "derived"
                },
                set(ANALYSIS_REPORT_FILENAMES.values()),
            )

    def test_match_cli_is_default_off_before_artifact_access(self) -> None:
        stderr = io.StringIO()
        with (
            mock.patch(
                "music_manager.matcher.validate_artifact_set",
                side_effect=AssertionError("artifact validation accessed"),
            ) as validate,
            mock.patch(
                "music_manager.musicbrainz_orchestration.ProductionMusicBrainzClient",
                side_effect=AssertionError("client accessed"),
            ) as client_factory,
            mock.patch(
                "music_manager.musicbrainz_orchestration."
                "register_musicbrainz_artifacts",
                side_effect=AssertionError("matching artifact accessed"),
            ) as register,
            redirect_stderr(stderr),
        ):
            exit_code = main(
                [
                    "match",
                    "--scan-run",
                    "/private/nonexistent/run",
                ]
            )

        self.assertEqual(exit_code, 2)
        validate.assert_not_called()
        client_factory.assert_not_called()
        register.assert_not_called()
        self.assertIn("MusicBrainz is disabled", stderr.getvalue())

    def test_match_cli_explicit_consent_runs_full_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = root / "12345678-1234-4abc-8def-1234567890ab"
            shutil.copytree(
                PROJECT_ROOT / "tests" / "fixtures" / "v0_3" / "valid",
                run_directory,
            )
            client = _EmptyMusicBrainzClient()
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch(
                    "music_manager.musicbrainz_orchestration."
                    "ProductionMusicBrainzClient",
                    return_value=client,
                ) as factory,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "match",
                        "--scan-run",
                        str(run_directory),
                        "--musicbrainz",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            factory.assert_called_once()
            self.assertEqual(client.calls, ["album", "recording"])
            self.assertTrue(client.closed)
            self.assertIn("Consent source: cli", stdout.getvalue())
            self.assertIn("music-manager/0.3.0", stdout.getvalue())
            self.assertIn("Album groups: 1", stdout.getvalue())
            self.assertIn("Recordings: 1", stdout.getvalue())
            self.assertIn("Candidates: 0", stdout.getvalue())
            self.assertIn("Unmatched: 2", stdout.getvalue())
            self.assertIn("Errors: 0", stdout.getvalue())
            artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(
                {
                    "musicbrainz_album_groups",
                    "musicbrainz_album_candidates",
                    "musicbrainz_recording_candidates",
                    "musicbrainz_match_results",
                },
                {
                    name
                    for name in artifacts.manifest.artifacts
                    if name.startswith("musicbrainz_")
                },
            )

    def test_no_musicbrainz_cli_override_blocks_enabled_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "music-manager.yml"
            config_path.write_text(
                "musicbrainz:\n  enabled: true\n",
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with (
                mock.patch(
                    "music_manager.matcher.validate_artifact_set",
                    side_effect=AssertionError("artifact validation accessed"),
                ) as validate,
                mock.patch(
                    "music_manager.musicbrainz_orchestration."
                    "ProductionMusicBrainzClient",
                    side_effect=AssertionError("client accessed"),
                ) as client_factory,
                mock.patch(
                    "music_manager.musicbrainz_orchestration."
                    "register_musicbrainz_artifacts",
                    side_effect=AssertionError("matching artifact accessed"),
                ) as register,
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "match",
                        "--scan-run",
                        "/private/nonexistent/run",
                        "--config",
                        str(config_path),
                        "--no-musicbrainz",
                    ]
                )

            self.assertEqual(exit_code, 2)
            validate.assert_not_called()
            client_factory.assert_not_called()
            register.assert_not_called()
            self.assertIn("MusicBrainz is disabled", stderr.getvalue())

    def test_match_cli_accepts_persistent_config_consent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = root / "12345678-1234-4abc-8def-1234567890ab"
            shutil.copytree(
                PROJECT_ROOT / "tests" / "fixtures" / "v0_3" / "valid",
                run_directory,
            )
            config_path = root / "music-manager.yml"
            config_path.write_text(
                "musicbrainz:\n  enabled: true\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            client = _EmptyMusicBrainzClient()

            with (
                mock.patch(
                    "music_manager.musicbrainz_orchestration."
                    "ProductionMusicBrainzClient",
                    return_value=client,
                ),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "match",
                        "--scan-run",
                        str(run_directory),
                        "--config",
                        str(config_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Consent source: config", stdout.getvalue())
            self.assertIn("MusicBrainz matching complete", stdout.getvalue())
            self.assertEqual(client.calls, ["album", "recording"])
            self.assertTrue(client.closed)

    def test_match_cli_registers_errors_before_nonzero_exit(self) -> None:
        private_values = (
            "private query",
            "private response body",
            "private-user",
            "private-host",
            "private-cache-path",
            "private-audio-data",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = root / "12345678-1234-4abc-8def-1234567890ab"
            shutil.copytree(
                PROJECT_ROOT / "tests" / "fixtures" / "v0_3" / "valid",
                run_directory,
            )
            client = _EmptyMusicBrainzClient(
                album_error=MusicBrainzRequestError(" ".join(private_values))
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch(
                    "music_manager.musicbrainz_orchestration."
                    "ProductionMusicBrainzClient",
                    return_value=client,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "match",
                        "--scan-run",
                        str(run_directory),
                        "--musicbrainz",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertTrue(client.closed)
            self.assertEqual(client.calls, ["album", "recording"])
            self.assertIn("Errors: 1", stdout.getvalue())
            self.assertIn("reports were registered", stderr.getvalue())
            artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(
                len(artifacts.musicbrainz_match_result_rows),
                2,
            )
            terminal = stdout.getvalue() + stderr.getvalue()
            for private_value in private_values:
                self.assertNotIn(private_value, terminal)

    def test_config_enabled_scan_and_analysis_do_not_open_musicbrainz(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_directory = root / "12345678-1234-4abc-8def-1234567890ab"
            shutil.copytree(
                PROJECT_ROOT / "tests" / "fixtures" / "v0_3" / "valid",
                run_directory,
            )
            config_path = root / "music-manager.yml"
            config_path.write_text(
                "musicbrainz:\n  enabled: true\n",
                encoding="utf-8",
            )
            source = root / "source"
            source.mkdir()
            scan_reports = root / "scan-reports"
            stdout = io.StringIO()

            with (
                mock.patch(
                    "music_manager.musicbrainz_orchestration."
                    "ProductionMusicBrainzClient",
                    side_effect=AssertionError("client accessed"),
                ) as client_factory,
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    scan_reports,
                ),
                redirect_stdout(stdout),
            ):
                scan_exit_code = main(
                    [
                        "scan",
                        "--source",
                        str(source),
                        "--config",
                        str(config_path),
                    ]
                )
                analysis_exit_code = main(
                    [
                        "analyze",
                        "--scan-run",
                        str(run_directory),
                        "--config",
                        str(config_path),
                    ]
                )

            self.assertEqual(scan_exit_code, 0)
            self.assertEqual(analysis_exit_code, 0)
            client_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
