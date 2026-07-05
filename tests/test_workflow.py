"""End-to-end tests for the synthetic scan and analysis workflow."""

from __future__ import annotations

import io
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from music_manager.artifact_schema import validate_artifact_set
from music_manager.cli import main


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


if __name__ == "__main__":
    unittest.main()
