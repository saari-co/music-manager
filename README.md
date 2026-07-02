# Music Manager

Music Manager is a local-first Python tool for scanning and organizing large,
messy music libraries.

Version 0.1.0 is intentionally limited to discovery and reporting. It reads a
local source folder recursively, extracts available metadata, and writes a CSV
inventory to `reports/library_scan.csv`.

## Safety model

The v0.1.0 scanner is read-only with respect to the source library. It does not:

- rename, move, copy, or delete files;
- edit or retag audio files;
- upload music or metadata; or
- make organization decisions on the user's behalf.

The only file it writes is the local CSV report. Unreadable or malformed files
are recorded as errors where possible instead of stopping the scan.

## Current capabilities

- Recursively discovers MP3, FLAC, M4A, AAC, and WAV audio files.
- Records file details, common tags, bitrate, and duration using Mutagen.
- Detects ZIP archives without extracting or modifying them.
- Identifies likely loose tracks using the number of audio files in their
  immediate folder.
- Prints a summary after writing the report.

## Requirements

- Python 3.9 or newer
- [Mutagen](https://mutagen.readthedocs.io/)

## Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows PowerShell, activate it with:

```powershell
.\.venv\Scripts\Activate.ps1
```

Install the pinned dependency:

```bash
python -m pip install -r requirements.txt
```

This installs `mutagen`, the library used to read audio metadata.

## Usage

Run the scanner with a source music folder:

```bash
python scripts/scan_library.py --source /path/to/music
```

The scanner creates `reports/` when needed and writes:

```text
reports/library_scan.csv
```

The report includes file paths, file size, folder depth, selected tags, bitrate,
duration, loose-track detection, archive detection, and per-file errors.

## Reports and privacy

Generated CSV and HTML reports are ignored by Git because they may contain
local file paths, artist names, album names, and other private library details.
Review and sanitize reports before sharing them anywhere.

## Public repository warning

Do not commit music files, archives containing music, generated reports,
copyrighted artwork, or metadata exports that you do not have permission to
publish. Remove personal paths and other identifying information from issue
reports, logs, screenshots, and pull requests.

## Project status

Music Manager v0.1.0 provides scanning and reporting only. Planned work is
documented in [ROADMAP.md](ROADMAP.md).

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines. Report
security concerns according to [SECURITY.md](SECURITY.md).

## License

Music Manager is available under the [MIT License](LICENSE).
