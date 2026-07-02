# Music Manager

Music Manager is an open source application for transforming large,
inconsistent music collections into verified, organized libraries without
risking the original files.

The project emphasizes safety, reproducibility, metadata accuracy, and staged
workflows. Every operation should analyze first, report second, and modify only
after explicit user approval.

## Project vision

Music Manager is intended to become a trusted local application for scanning,
analyzing, verifying, staging, organizing, and reporting on very large music
collections. Each result should be explainable, reproducible, and reviewable
before it can affect a library.

Version 0.1 provides the safe foundation: a read-only scanner and CSV report.
Later milestones build on that foundation instead of bypassing it.

## Why Music Manager exists

Long-lived music collections accumulate inconsistent tags, duplicate files,
mixed audio quality, partial albums, archives, and uncertain folder structures.
Ad hoc cleanup scripts make those problems harder when they modify files
without a reviewable plan.

Music Manager separates discovery from decisions. It creates structured
evidence first, then supports explicit and verifiable workflows for any future
change.

## Core principles

- **Local-first:** library data remains on the user's machine unless a clearly
  documented feature requires an explicit external request.
- **Read-only by default:** analysis never implies permission to modify files.
- **Explicit approval:** future write operations must show their plan and wait
  for confirmation.
- **Staged and verifiable:** organization work belongs in a separate staging
  library with checksum validation.
- **Reproducible:** reports and operation logs should explain every result.
- **Privacy-conscious:** music, reports, personal paths, and private metadata
  never belong in the public repository.
- **Traceable development:** features move through issues, milestones, branches,
  pull requests, changelog entries, tags, and releases.

## Current capabilities

Music Manager v0.1 can:

- recursively discover MP3, FLAC, M4A, AAC, and WAV files;
- read common tags, bitrate, duration, file size, and folder depth with Mutagen;
- identify likely loose tracks;
- detect ZIP archives without opening or extracting them;
- continue past unreadable files and record errors; and
- write a local CSV inventory and print a scan summary.

## Planned capabilities

Planned milestones add:

- duplicate detection, quality scoring, corruption checks, and folder analysis;
- MusicBrainz matching with confidence scores and metadata verification;
- checksum-verified staging copies;
- approved renaming, retagging, artwork, and album normalization in staging;
- a local HTML dashboard; and
- continuous processing of a dedicated inbox.

See [ROADMAP.md](ROADMAP.md) for milestone scope.

## Installation

Music Manager requires Python 3.9 or newer.

```bash
git clone https://github.com/saari-co/music-manager.git
cd music-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Quick start

Run the application against a local source folder:

```bash
python -m music_manager --source /path/to/music
```

The compatibility launcher remains available:

```bash
python scripts/scan_library.py --source /path/to/music
```

Both commands write `reports/library_scan.csv`. Generated reports are ignored
by Git because they can contain local paths and private library metadata.

## Development

Create a feature branch before making changes:

```bash
git switch main
git pull --ff-only
git switch -c feature/short-description
```

Run the automated tests and syntax checks:

```bash
python -m unittest discover -s tests -v
python -m compileall -q music_manager scripts tests
```

Application logic belongs in `music_manager/`. Files in `scripts/` are
compatibility launchers only. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
branch, pull request, labeling, verification, and release workflow.

## Safety model

Version 0.1 does not rename, move, copy, delete, retag, upload, or otherwise
modify music files. It reads the selected source and writes only a local report.
Unreadable files become report errors instead of terminating the scan.

Future capabilities that can write data must operate on a separate staging
library, present a reviewable plan, require explicit approval, and verify their
results. Source-library mutation is outside the default safety model.

Never commit or publicly attach audio, copyrighted artwork, ZIP archives,
generated reports, personal paths, or private metadata. Sanitize issue and pull
request content before sharing it.

Security reporting guidance is available in [SECURITY.md](SECURITY.md).

## Roadmap summary

| Milestone | Focus |
| --- | --- |
| v0.1 | Read-only scanning and CSV reporting |
| v0.2 | Duplicate, quality, corruption, and folder analysis |
| v0.3 | MusicBrainz matching and metadata confidence |
| v0.4 | Checksum-verified staging library |
| v0.5 | Safe organization engine for staged copies |
| v0.6 | Local HTML dashboard |
| v0.7 | Continuous inbox automation |
| v1.0 | Stable, trusted application workflows |

## License

Music Manager is available under the [MIT License](LICENSE).
