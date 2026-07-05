"""End-to-end tests for the synthetic scan and analysis workflow."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
import wave
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from music_manager.artifact_schema import validate_artifact_set
from music_manager.cli import main


class _FakeInfo:
    bitrate = 192000
    length = 180.0
    sample_rate = 44100
    bits_per_sample = 16
    channels = 2


class _FakeAudio:
    tags = {
        "artist": ["Synthetic Artist"],
        "title": ["Synthetic Track"],
        "album": ["Synthetic Album"],
        "date": ["2026"],
        "tracknumber": ["1"],
    }
    info = _FakeInfo()


def _source_snapshot(source: Path) -> dict[str, tuple[str, object]]:
    snapshot: dict[str, tuple[str, object]] = {}
    for path in sorted(source.rglob("*"), key=lambda item: str(item)):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", path.stat().st_mode)
        else:
            snapshot[relative] = ("file", path.read_bytes())
    return snapshot


class WorkflowTests(unittest.TestCase):
    """Verify the release workflow without using a real music library."""

    def test_versioned_scan_preserves_source_and_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "source"
            audio_path = source / "Artist" / "Album" / "01 Synthetic.wav"
            audio_path.parent.mkdir(parents=True)
            with wave.open(str(audio_path), "wb") as audio_file:
                audio_file.setnchannels(1)
                audio_file.setsampwidth(2)
                audio_file.setframerate(8000)
                audio_file.writeframes(b"\x00\x00" * 800)

            original_audio = audio_path.read_bytes()
            reports = root / "reports"
            stdout = io.StringIO()

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports,
                ),
                redirect_stdout(stdout),
            ):
                scan_exit_code = main(["scan", "--source", str(source)])

            run_directories = [path for path in reports.iterdir() if path.is_dir()]
            self.assertEqual(len(run_directories), 1)
            run_directory = run_directories[0]
            scan_report = run_directory / "library_scan.csv"

            self.assertEqual(scan_exit_code, 0)
            self.assertEqual(audio_path.read_bytes(), original_audio)
            self.assertTrue(scan_report.is_file())
            self.assertTrue((run_directory / "scan_manifest.json").is_file())
            self.assertTrue((run_directory / "scan_errors.csv").is_file())
            self.assertNotIn(
                str(root),
                scan_report.read_text(encoding="utf-8"),
            )

            for report_path in run_directory.iterdir():
                if report_path.is_file():
                    self.assertNotIn(
                        str(root),
                        report_path.read_text(encoding="utf-8"),
                    )

            for filename in (
                "library_analysis.csv",
                "duplicate_candidates.csv",
                "missing_metadata.csv",
                "corrupt_files.csv",
                "quality_summary.csv",
            ):
                report_path = run_directory / filename
                self.assertFalse(report_path.exists())
                self.assertNotIn(
                    filename,
                    (run_directory / "scan_manifest.json").read_text(encoding="utf-8"),
                )

            output = stdout.getvalue()
            self.assertIn("Scan complete", output)
            self.assertIn(f"Reports directory: {run_directory}", output)
            self.assertIn("Scan state: complete", output)

            analysis_stdout = io.StringIO()
            with redirect_stdout(analysis_stdout):
                analysis_exit_code = main(["analyze", "--scan-run", str(run_directory)])

            self.assertEqual(analysis_exit_code, 0)
            self.assertEqual(audio_path.read_bytes(), original_audio)
            artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(artifacts.manifest.state, "complete")
            self.assertEqual(
                {
                    entry.filename
                    for entry in artifacts.manifest.artifacts.values()
                    if entry.role == "derived"
                },
                {
                    "library_analysis.csv",
                    "duplicate_candidates.csv",
                    "missing_metadata.csv",
                    "corrupt_files.csv",
                    "quality_summary.csv",
                },
            )
            for report_path in run_directory.iterdir():
                if report_path.is_file():
                    self.assertNotIn(
                        str(root),
                        report_path.read_text(encoding="utf-8"),
                    )
            self.assertIn("Analysis complete", analysis_stdout.getvalue())
            self.assertIn(
                f"Reports directory: {run_directory}",
                analysis_stdout.getvalue(),
            )

    def test_private_roots_and_symlink_targets_never_reach_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            source = root / "Private Library"
            real_directory = source / "Real"
            ignored_directory = source / "Ignored"
            outside = root / "Outside Library"
            real_directory.mkdir(parents=True)
            ignored_directory.mkdir()
            outside.mkdir()

            good_audio = real_directory / "Good.mp3"
            bad_audio = source / "Unreadable.mp3"
            outside_audio = outside / "Outside.mp3"
            good_audio.write_bytes(b"synthetic-good")
            bad_audio.write_bytes(b"synthetic-bad")
            outside_audio.write_bytes(b"synthetic-outside")
            (ignored_directory / "Ignored.mp3").write_bytes(b"synthetic-ignored")

            os.symlink(good_audio, source / "File Link.mp3")
            os.symlink(real_directory, source / "Directory Link")
            os.symlink(outside / "Missing.mp3", source / "Broken.mp3")
            os.symlink(outside_audio, source / "Outside.mp3")
            os.symlink(source, source / "Cycle")

            config_path = root / "private-config.yml"
            config_path.write_text(
                "path_mode: relative\nignore:\n  - Ignored\n",
                encoding="utf-8",
            )
            source_before = _source_snapshot(source)
            config_before = config_path.read_bytes()
            reports = root / "reports"
            metadata_paths: list[Path] = []
            protected_targets = {
                outside_audio,
                outside / "Missing.mp3",
            }
            original_open = Path.open
            original_stat = Path.stat

            def guarded_open(path: Path, *args: object, **kwargs: object):
                if path in protected_targets:
                    self.fail(f"opened symlink target: {path}")
                return original_open(path, *args, **kwargs)

            def guarded_stat(path: Path, *args: object, **kwargs: object):
                if path in protected_targets:
                    self.fail(f"statted symlink target: {path}")
                return original_stat(path, *args, **kwargs)

            def load_metadata(path: Path) -> _FakeAudio:
                metadata_paths.append(path)
                if path == bad_audio:
                    raise OSError(f"cannot read {path} under {source}")
                return _FakeAudio()

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports,
                ),
                mock.patch(
                    "music_manager.cli.metadata_reader_available",
                    return_value=True,
                ),
                mock.patch(
                    "music_manager.scanner._load_metadata",
                    side_effect=load_metadata,
                ),
                mock.patch.object(Path, "open", guarded_open),
                mock.patch.object(Path, "stat", guarded_stat),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
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

            self.assertEqual(scan_exit_code, 0)
            self.assertEqual(len(metadata_paths), 2)
            self.assertEqual(set(metadata_paths), {good_audio, bad_audio})
            self.assertEqual(metadata_paths.count(good_audio), 1)
            self.assertEqual(metadata_paths.count(bad_audio), 1)
            self.assertEqual(_source_snapshot(source), source_before)
            self.assertEqual(config_path.read_bytes(), config_before)

            run_directories = [path for path in reports.iterdir() if path.is_dir()]
            self.assertEqual(len(run_directories), 1)
            run_directory = run_directories[0]
            artifacts = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(artifacts.manifest.state, "incomplete")
            self.assertEqual(
                {
                    row.path
                    for row in artifacts.error_rows
                    if row.error_code == "symlink_skipped"
                },
                {
                    "Broken.mp3",
                    "Cycle",
                    "Directory Link",
                    "File Link.mp3",
                    "Outside.mp3",
                },
            )
            self.assertEqual(artifacts.manifest.counts.skipped_symlinks, 5)
            self.assertEqual(
                artifacts.manifest.configuration.ignore,
                ("Ignored",),
            )
            self.assertIn("Scan state: incomplete", stdout.getvalue())
            self.assertIn("could not read", stderr.getvalue())

            for artifact_path in run_directory.iterdir():
                if artifact_path.is_file():
                    self.assertNotIn(
                        str(root),
                        artifact_path.read_text(encoding="utf-8"),
                    )

            shutil.rmtree(source)
            shutil.rmtree(outside)
            config_path.unlink()
            analysis_stdout = io.StringIO()
            with redirect_stdout(analysis_stdout):
                analysis_exit_code = main(["analyze", "--scan-run", str(run_directory)])

            self.assertEqual(analysis_exit_code, 0)
            analyzed = validate_artifact_set(run_directory / "scan_manifest.json")
            self.assertEqual(analyzed.manifest.state, "incomplete")
            self.assertTrue(
                any(
                    entry.role == "derived"
                    for entry in analyzed.manifest.artifacts.values()
                )
            )
            for artifact_path in run_directory.iterdir():
                if artifact_path.is_file():
                    self.assertNotIn(
                        str(root),
                        artifact_path.read_text(encoding="utf-8"),
                    )


if __name__ == "__main__":
    unittest.main()
