# Roadmap

This roadmap describes the intended direction of Music Manager. Scope and
ordering may change as safety requirements and real-world library behavior
become better understood.

## Phase 1: Discovery and reporting

- Maintain a read-only recursive library scanner.
- Improve metadata coverage, report clarity, and test fixtures.
- Document ambiguous files and incomplete tags without modifying source data.

## Phase 2: Duplicate detection and quality analysis

- Detect exact duplicates using cryptographic hashes.
- Identify likely duplicates using metadata and audio characteristics.
- Compare format, bitrate, duration, and completeness without auto-deleting.

## Phase 3: MusicBrainz matching and confidence scoring

- Query MusicBrainz using privacy-conscious, user-initiated workflows.
- Rank possible recording and release matches.
- Expose confidence and ambiguity instead of silently selecting metadata.

## Phase 4: Staging library copy with checksum validation

- Copy selected files into a separate staging library.
- Verify every staged copy with checksums.
- Preserve the source library as read-only.

## Phase 5: Safe retagging and renaming in staging only

- Apply approved metadata and naming rules only to staged copies.
- Preview changes and retain an auditable operation log.
- Validate staged files after each operation.

## Phase 6: HTML dashboard

- Provide a local report dashboard for filtering and review.
- Visualize missing metadata, duplicates, quality, and match confidence.
- Keep generated dashboards out of version control by default.

## Phase 7: Continuous inbox imports

- Scan a dedicated inbox for newly added music.
- Reuse duplicate, matching, staging, and validation workflows.
- Require explicit approval before promotion into an organized library.

## Phase 8: Optional desktop app wrapper

- Package the local workflows behind a desktop interface.
- Keep the command-line tool available and independently usable.
- Preserve the same safety guarantees and review steps.
