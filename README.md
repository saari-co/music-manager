# Music Manager

Music Manager is an open source, local-first command-line application for
inventorying and analyzing large, inconsistent music collections without
modifying the original files.

The project emphasizes safety, reproducibility, metadata accuracy, and staged
workflows. Current operations analyze and report only. Any future write
operation must present a reviewable plan and require explicit user approval.

The current release is **v0.2.1**.

## Project vision

Music Manager is intended to become a trusted local application for scanning,
analyzing, verifying, staging, organizing, and reporting on very large music
collections. Staging and organization are future capabilities. Each result
should be explainable, reproducible, and reviewable before it can affect a
library.

The released v0.2 workflow provides a read-only scanner, CSV inventory, and
report-only analysis. Later milestones build on that foundation instead of
bypassing it.

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
- **Staged and verifiable:** future organization work belongs in a separate
  staging library with checksum validation.
- **Reproducible:** reports and operation logs should explain every result.
- **Privacy-conscious:** music, reports, personal paths, and private metadata
  never belong in the public repository.
- **Traceable development:** features move through issues, milestones, branches,
  pull requests, changelog entries, tags, and releases.

## Current capabilities

The current scanner can:

- recursively discover MP3, FLAC, M4A, AAC, and WAV files;
- treat every supported audio file under the selected scan root as part of one
  Root Library;
- read common tags, bitrate, duration, and file size with Mutagen;
- detect ZIP archives without opening or extracting them;
- continue past unreadable files and record errors; and
- write a local CSV inventory and print a scan summary.

The v0.2 analysis layer can read that scan CSV and:

- prioritize duplicate candidates using normalized artist, title, and
  duration, including matches found in different folders;
- summarize bitrate ranges while separating unknown and lossless files;
- identify readable files with missing metadata;
- isolate corrupt or unreadable scan rows;
- calculate metadata completeness percentages;
- write focused CSV reports without opening any referenced music file.

The scanner does not classify folders by music app, artist folder, download
folder, or any other source guess. Every supported audio file under the
selected scan root is part of the Root Library. If similar or matching files
appear in different folders, they are handled through duplicate detection
only.

## Planned capabilities

Planned milestones, none of which are implemented yet, add:

- v0.3: a durable, versioned scan and report contract;
- v0.4: opt-in MusicBrainz matching with confidence scores;
- v0.5: checksum-verified staging copies;
- v0.6: approved renaming, retagging, artwork, and album normalization of
  staged copies;
- v0.7: a local HTML dashboard; and
- v0.8: continuous processing of a dedicated inbox.

See [ROADMAP.md](ROADMAP.md) for milestone scope.

## Installation

Music Manager supports CPython 3.11, 3.12, 3.13, and 3.14. These versions are
covered by the continuous integration test matrix. Confirm that `python3`
resolves to one of these versions before creating the virtual environment:

```bash
python3 --version
git clone https://github.com/saari-co/music-manager.git
cd music-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

On Windows PowerShell, activate the environment with:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Quick start

Run the installed application against a local source folder:

```bash
music-manager scan --source /path/to/music
```

The existing module entry point remains available:

```bash
python -m music_manager scan --source /path/to/music
```

The compatibility launcher remains available:

```bash
python scripts/scan_library.py --source /path/to/music
```

All three scan commands write `reports/library_scan.csv`. The installed command
also accepts the original `music-manager --source /path/to/music` form for
compatibility.

Analyze an existing scan:

```bash
music-manager analyze \
  --scan-report reports/library_scan.csv
```

Analysis writes:

- `reports/library_analysis.csv`
- `reports/duplicate_candidates.csv`
- `reports/missing_metadata.csv`
- `reports/corrupt_files.csv`
- `reports/quality_summary.csv`

The entire `reports/` directory is ignored by Git because generated reports can
contain local paths and private library metadata. Sanitized, synthetic examples
live under [`examples/`](examples/).

### Paths and local configuration

Generated CSV paths are relative by default. Use absolute paths only when a
local workflow specifically requires them:

```bash
music-manager analyze \
  --scan-report reports/library_scan.csv \
  --path-mode absolute
```

Copy the example configuration to set persistent local defaults:

```bash
cp music-manager.example.yml music-manager.yml
```

```yaml
path_mode: relative
ignore:
  - .DS_Store
  # - Music/Media.localized
```

`path_mode` accepts `relative` or `absolute`. Ignore patterns are evaluated
relative to the selected scan root and prune matching files or directories.
The local `music-manager.yml` file is ignored by Git to prevent accidental
publication of machine-specific rules.

`Root Library total` is the count of all supported audio files found anywhere
under the selected scan root, including unreadable files. Folder placement
does not create additional libraries or categories.

## Development

Create a feature branch before making changes:

```bash
git switch main
git pull --ff-only
git switch -c feature/short-description
```

Run the automated tests and syntax checks:

```bash
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q music_manager scripts tests
```

Application logic belongs in `music_manager/`. Files in `scripts/` are
compatibility launchers only. See [CONTRIBUTING.md](CONTRIBUTING.md) for the
branch, pull request, labeling, verification, and release workflow.

## Safety model

Scanning and analysis do not rename, move, copy, delete, retag, upload, or
otherwise modify music files. The scanner reads the selected source and writes
only a local report. Reports use relative paths by default. The analyzer reads
that report without opening the music paths it contains, then writes local
analysis reports. Unreadable files become report errors instead of terminating
the workflow.

Future capabilities that can write data must operate on a separate staging
library, present a reviewable plan, require explicit approval, and verify their
results. Source-library mutation is outside the default safety model.

Never commit or publicly attach audio, copyrighted artwork, ZIP archives,
generated reports, personal paths, or private metadata. Sanitize issue and pull
request content before sharing it.

Security reporting guidance is available in [SECURITY.md](SECURITY.md).

## Roadmap summary

| Milestone | Status | Focus |
| --- | --- | --- |
| v0.1 | Released | Read-only scanning and CSV reporting |
| v0.2 | Released | Duplicate-first library audit, quality, and corruption analysis |
| v0.2.1 | Released | Packaging, CLI, development, configuration, documentation, and test cleanup |
| v0.3 | Planned | Durable scan and report contract |
| v0.4 | Planned | Opt-in MusicBrainz matching and metadata confidence |
| v0.5 | Planned | Checksum-verified staging library |
| v0.6 | Planned | Safe organization engine for staged copies |
| v0.7 | Planned | Local HTML dashboard |
| v0.8 | Planned | Continuous inbox automation |
| v1.0 | Planned | Stable, trusted application workflows |

## License

Music Manager is available under the [MIT License](LICENSE).
