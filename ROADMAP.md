# Roadmap

Music Manager is developed through versioned milestones. Each milestone should
have corresponding GitHub issues, a milestone, reviewed pull requests,
changelog entries, a version tag, and release notes.

Scope may change as safety requirements and real-world library behavior become
better understood. No milestone may weaken the local-first, read-only-default,
or explicit-approval principles.

## v0.1 — Read-only Scanner

**Status:** Released

- Recursively discover supported audio files and ZIP archives.
- Extract common metadata, bitrate, duration, and file size.
- Write a CSV inventory without changing source files.
- Continue past unreadable files and provide a terminal summary.

## v0.2 — Library Analysis

**Status:** Released

- Analyze an existing `library_scan.csv` without accessing music files.
- Report duplicate candidates using normalized artist, title, and duration.
- Summarize bitrate ranges, unknown bitrates, and lossless formats.
- Report missing artist, title, album, year, and track number values.
- Calculate per-field metadata completeness percentages.
- Extract corrupt and unreadable scan rows into a focused report.
- Treat all supported audio files under one selected scan root as the Root
  Library without classifying folders by name.
- Use relative report paths by default with a configurable absolute mode.
- Support YAML configuration for path mode and scan ignore patterns.
- Cover analysis behavior with synthetic CSV fixtures.

Report outputs:

- `library_analysis.csv`
- `duplicate_candidates.csv`
- `missing_metadata.csv`
- `corrupt_files.csv`
- `quality_summary.csv`

All findings remain read-only and report-driven. Duplicate reports identify
candidates only and do not recommend deletion.

## v0.2.1 — Foundation Cleanup

**Status:** Planned

- Add `pyproject.toml`.
- Add an installable console command.
- Add development dependencies for testing and linting.
- Reject unknown configuration keys.
- Update stale documentation.
- Add regression tests for current scan and analysis behavior.

This patch does not add network access, staging, retagging, renaming, or
destructive behavior.

## v0.3 — Durable Scan and Report Contract

**Status:** Planned

- Write a short format decision document before implementation.
- Add a versioned `scan_manifest.json`.
- Add a structured `scan_errors.csv`.
- Write each run to a dedicated `reports/<scan-id>/` directory.
- Define manifest completion states for complete, incomplete, and failed runs.
- Record the report schema version, application version, and scan ID.
- Record a sanitized configuration snapshot.
- Define a root-path privacy policy that keeps portable reports free of
  private absolute paths.
- Define a relative-path contract for scan and analysis artifacts.
- Document and support legacy unversioned report handling.
- Define symlink behavior, including safe defaults and persisted findings.
- Expand metadata fields before external matching.
- Define `file_record_id` semantics for stable report-row identity.
- Define `file_fingerprint` semantics for file change detection.
- Enforce strict CSV validation according to schema compatibility rules.
- Add regression, compatibility, failure, and scale tests.

All behavior remains local-first, read-only, and report-driven.

## v0.4 — MusicBrainz Integration

**Status:** Planned

- Require explicit opt-in before any network access.
- Send an identifiable application User-Agent.
- Use a cached MusicBrainz client.
- Apply deterministic rate limiting.
- Implement retry and backoff behavior.
- Produce album and recording candidates.
- Record evidence used to evaluate each candidate.
- Provide explainable confidence scoring.
- Report ambiguous and unmatched results.
- Cover matching behavior with fully mocked offline tests.

No metadata is applied automatically.

## v0.5 — Safe Staging Library

**Status:** Planned

- Copy approved files into a separate staging library.
- Validate source and staged files with checksums.
- Verify copies before later operations can proceed.
- Preserve the source library without destructive operations.

## v0.6 — Organization Engine

**Status:** Planned

- Previewed rename plans.
- Safe retagging of staged copies.
- Artwork review and application.
- Album and folder normalization.
- Auditable operation logs and post-operation validation.

Organization is limited to verified staging copies and requires explicit user
approval.

## v0.7 — HTML Dashboard

**Status:** Planned

Generate a local dashboard with:

- `index.html`
- `duplicates.html`
- `albums.html`
- `corrupt_files.html`
- `missing_artwork.html`
- `unmatched_tracks.html`

Generated dashboards remain local and ignored by Git.

## v0.8 — Inbox Automation

**Status:** Planned

- Detect newly added music in a dedicated inbox.
- Run the established scan, analysis, matching, and staging pipeline.
- Require approval before promotion into an organized library.
- Record repeatable import history.

## v1.0 — Stable Release

**Status:** Planned

- Stable command-line and application interfaces.
- Documented migrations and compatibility guarantees.
- End-to-end safety validation for supported workflows.
- Reliable release, upgrade, and recovery documentation.
