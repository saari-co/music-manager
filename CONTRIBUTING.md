# Contributing

Contributions that improve safety, portability, metadata handling, tests, or
documentation are welcome.

## Development workflow

Never develop directly on `main`.

Create and activate a virtual environment, then install the project with its
development tools:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --editable ".[dev]"
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1` instead.

1. Create or select a GitHub issue for the change.
2. Associate the issue with the appropriate roadmap milestone.
3. Create a focused branch from the latest `main`.
4. Make one coherent change and keep unrelated edits out of the branch.
5. Add tests or clear manual verification notes.
6. Update documentation and `CHANGELOG.md` when behavior changes.
7. Open a pull request that links the issue and describes the safety impact.
8. Merge only after review and required checks pass.

Use descriptive branch names such as:

- `feature/musicbrainz`
- `feature/html-dashboard`
- `feature/duplicate-analysis`
- `fix/unreadable-file-reporting`
- `refactor/scanner-boundaries`

Keep pull requests small enough to review and verify. Releases should be tagged
and accompanied by changelog entries and GitHub release notes.

## Application boundaries

Reusable application logic belongs in `music_manager/`. The `scripts/`
directory contains small compatibility launchers and should not become a second
application layer.

New features should respect existing module responsibilities or propose a clear
architectural change in the pull request. Source-library behavior must remain
read-only. Any future write operation must be limited to a separate staging
library, present a reviewable plan, and require explicit user approval.

## Verification

Changes must include automated tests when practical. If automated coverage is
not practical, include clear manual verification notes in the pull request.

At minimum, run the checks used in continuous integration:

```bash
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q music_manager scripts tests
```

Scanner changes should also be tested against non-sensitive sample files that
you created or are licensed to redistribute.

## Recommended labels

Use labels consistently so work can be discovered and included in milestones:

- `bug`
- `documentation`
- `enhancement`
- `good first issue`
- `help wanted`
- `performance`
- `refactor`
- `testing`
- `scanner`
- `duplicates`
- `musicbrainz`
- `dashboard`
- `ui`

## Privacy and copyright

- Never attach or commit copyrighted audio files to issues, pull requests,
  fixtures, or commits.
- Do not commit generated library reports or personal file paths.
- Sanitize logs, screenshots, metadata, and reproduction steps.
- Use synthetic or explicitly redistributable fixtures only.

By submitting a contribution, you agree that it may be distributed under the
project's MIT License.
