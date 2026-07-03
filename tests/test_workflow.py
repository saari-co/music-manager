"""End-to-end tests for the synthetic scan and analysis workflow."""

from __future__ import annotations

import io
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from music_manager.cli import main
from music_manager.reports import ANALYSIS_REPORT_FILENAMES


class WorkflowTests(unittest.TestCase):
    """Verify the release workflow without using a real music library."""

    def test_scan_then_analyze_preserves_source_and_private_paths(self) -> None:
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
            scan_report = reports / "library_scan.csv"
            stdout = io.StringIO()

            with (
                mock.patch(
                    "music_manager.cli.DEFAULT_REPORT_DIRECTORY",
                    reports,
                ),
                mock.patch(
                    "music_manager.cli.DEFAULT_SCAN_REPORT_PATH",
                    scan_report,
                ),
                redirect_stdout(stdout),
            ):
                scan_exit_code = main(["scan", "--source", str(source)])
                analysis_exit_code = main(
                    ["analyze", "--scan-report", str(scan_report)]
                )

            self.assertEqual(scan_exit_code, 0)
            self.assertEqual(analysis_exit_code, 0)
            self.assertEqual(audio_path.read_bytes(), original_audio)
            self.assertTrue(scan_report.is_file())
            self.assertNotIn(
                str(root),
                scan_report.read_text(encoding="utf-8"),
            )

            for filename in ANALYSIS_REPORT_FILENAMES.values():
                report_path = reports / filename
                self.assertTrue(report_path.is_file())
                self.assertNotIn(
                    str(root),
                    report_path.read_text(encoding="utf-8"),
                )

            output = stdout.getvalue()
            self.assertIn("Scan complete", output)
            self.assertIn("Analysis complete", output)


if __name__ == "__main__":
    unittest.main()
