# Contributing

Contributions that improve safety, portability, metadata handling, tests, or
documentation are welcome.

## Workflow

1. Create a focused branch from the current default branch.
2. Make one coherent change and keep unrelated edits out of the branch.
3. Run the relevant checks locally.
4. Open a pull request describing the behavior change and its safety impact.
5. Address review feedback before merging.

Do not push directly to the default branch. Keep pull requests small enough to
review and verify.

## Verification

Changes must include automated tests when practical. If automated coverage is
not practical, include clear manual verification notes in the pull request.

At minimum, run the syntax check used in continuous integration:

```bash
python -m py_compile scripts/scan_library.py
```

Scanner changes should also be tested against non-sensitive sample files that
you created or are licensed to redistribute.

## Privacy and copyright

- Never attach or commit copyrighted audio files to issues, pull requests,
  fixtures, or commits.
- Do not commit generated library reports or personal file paths.
- Sanitize logs, screenshots, metadata, and reproduction steps.
- Use synthetic or explicitly redistributable fixtures only.

By submitting a contribution, you agree that it may be distributed under the
project's MIT License.
