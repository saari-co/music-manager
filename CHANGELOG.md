# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-07-04

### Added

- Added `pyproject.toml` packaging support.
- Added the `music-manager` console command.
- Added development linting support with Ruff.
- Added release regression tests for CLI and current behavior.

### Changed

- Updated stale documentation to match the current released behavior and
  roadmap.

### Fixed

- Unknown YAML configuration keys now fail clearly instead of being silently
  ignored.

## [0.2.0] - 2026-07-02

### Added

- Added an `analyze` CLI command for existing library scan CSV files.
- Added duplicate candidate grouping with normalized metadata and configurable
  duration tolerance.
- Added focused reports for missing metadata, corrupt files, bitrate quality,
  and duplicate candidates.
- Added metadata completeness percentages for dashboard-ready summaries.
- Added YAML configuration for report path mode and scan ignore patterns.
- Added relative report paths by default with an explicit absolute-path mode.
- Added synthetic CSV tests for the complete analysis layer.
- Added an end-to-end synthetic scan and analysis workflow test.
- Added CI coverage for supported CPython versions 3.11 through 3.14.

### Changed

- Simplified standard output around one Root Library containing every supported
  audio file under the selected scan root.
- Removed source classification, loose-track reporting, and folder summaries
  from standard reports.
- Ignored the complete generated `reports/` directory and kept only sanitized
  report examples under `examples/`.

### Security

- Analysis operates only on CSV records and never opens or modifies referenced
  music files.
- Relative report paths and sanitized error text reduce accidental disclosure
  of usernames and home-directory locations.

## [0.1.1] - 2026-07-02

### Changed

- Restructured the scanner into a reusable `music_manager` application package.
- Reduced `scripts/scan_library.py` to a compatibility launcher.
- Added typed scan models, report boundaries, future feature modules, and an
  automated test foundation.
- Expanded project philosophy, milestone planning, and contribution workflow
  documentation.

## [0.1.0] - 2026-07-02

### Added

- Read-only recursive scanner for MP3, FLAC, M4A, AAC, and WAV files.
- CSV report containing file details, audio metadata, bitrate, duration, and
  likely loose-track status.
- ZIP archive detection without extraction.
- Per-file and per-directory error handling that allows scans to continue.
- Terminal summary with audio, archive, loose-track, and error counts.
