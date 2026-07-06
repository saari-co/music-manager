# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added schema 1.1 models and strict validators for the planned MusicBrainz
  matching artifact family while retaining schema 1.0 compatibility.
- Added default-off MusicBrainz configuration, consent preflight plumbing,
  identifiable User-Agent construction, and an injectable client interface
  without real network or matching behavior.
- Added the production MusicBrainz client shell with fixed HTTPS request
  policy, persistent opaque-key caching, deterministic rate limiting and
  retries, and fully injected offline test seams.
- Added deterministic in-memory MusicBrainz album and recording subject
  extraction plus injected-client candidate retrieval without scoring or
  artifact output.
- Added deterministic in-memory MusicBrainz evidence scoring, candidate
  ranking, confidence margins, and result classification without artifact
  output or matching activation.

## [0.3.0] - 2026-07-04

### Added

- Added the schema 1 manifest, inventory, structured-error, and derived-report
  contracts with strict validation and artifact integrity checks.
- Added exclusive `reports/<scan-id>/` run directories with atomic `running`,
  `complete`, `incomplete`, and `failed` lifecycle transitions.
- Added expanded metadata fields, scan-local file record IDs, stat
  fingerprints, and persisted findings for skipped symlinks.
- Added versioned analysis provenance and all-or-nothing derived artifact
  registration without accessing referenced source-library paths.
- Added strict, warned compatibility analysis for the two documented
  unversioned v0.2 scan headers without inventing schema 1 provenance.
- Added synthetic end-to-end, failure-injection, privacy, symlink, contract,
  and deterministic 100,000-row scale coverage.

### Changed

- Scans now create a versioned artifact directory instead of overwriting one
  flat inventory report.
- Versioned analysis now selects an explicit finalized scan run and registers
  its reports in that run's manifest.
- User documentation now explains versioned run selection and how to recognize
  legacy compatibility mode.

### Security

- Versioned artifacts require source-relative paths and sanitized configuration
  and error text so absolute roots do not enter durable reports.
- File, directory, broken, outside-root, and cyclic symlinks are never followed.
- Scanning and analysis remain local, read-only workflows with no network or
  source-library write behavior.

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
