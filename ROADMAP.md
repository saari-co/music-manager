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

**Status:** In development

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

Planned outputs:

- `library_analysis.csv`
- `duplicate_candidates.csv`
- `missing_metadata.csv`
- `corrupt_files.csv`
- `quality_summary.csv`

All findings remain read-only and report-driven. Duplicate reports identify
candidates only and do not recommend deletion.

## v0.3 — MusicBrainz Integration

**Status:** Planned

- Album and recording matching.
- Confidence scoring for candidate matches.
- Metadata verification and ambiguity reporting.
- User-controlled, privacy-conscious external lookups.

No metadata is applied automatically.

## v0.4 — Safe Staging Library

**Status:** Planned

- Copy approved files into a separate staging library.
- Validate source and staged files with checksums.
- Verify copies before later operations can proceed.
- Preserve the source library without destructive operations.

## v0.5 — Organization Engine

**Status:** Planned

- Previewed rename plans.
- Safe retagging of staged copies.
- Artwork review and application.
- Album and folder normalization.
- Auditable operation logs and post-operation validation.

Organization is limited to verified staging copies and requires explicit user
approval.

## v0.6 — HTML Dashboard

**Status:** Planned

Generate a local dashboard with:

- `index.html`
- `duplicates.html`
- `albums.html`
- `corrupt_files.html`
- `missing_artwork.html`
- `unmatched_tracks.html`

Generated dashboards remain local and ignored by Git.

## v0.7 — Inbox Automation

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
