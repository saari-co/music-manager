## Summary

Describe the problem and the resulting behavior.

## Linked issue and milestone

Link the issue this pull request resolves and name its roadmap milestone.

## Changes

- Describe the implementation changes.

## Verification

List automated checks and manual verification performed.

```text
ruff check .
python -m unittest discover -s tests -v
python -m compileall -q music_manager scripts tests
```

## Safety and privacy checklist

- [ ] This change was developed on a focused branch, not directly on `main`.
- [ ] Source-library operations remain read-only, or any changed behavior is
      explicitly documented and safely isolated.
- [ ] Tests or manual verification notes cover the change.
- [ ] No audio files, archives, generated reports, or copyrighted content are
      included.
- [ ] No personal paths, private metadata, credentials, or machine-specific
      files are included.
- [ ] Documentation and changelog entries are updated when needed.
- [ ] The linked issue, milestone, labels, and release impact are identified.
