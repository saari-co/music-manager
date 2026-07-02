## Summary

Describe the problem and the resulting behavior.

## Changes

- Describe the implementation changes.

## Verification

List automated checks and manual verification performed.

```text
python -m py_compile scripts/scan_library.py
```

## Safety and privacy checklist

- [ ] Source-library operations remain read-only, or any changed behavior is
      explicitly documented and safely isolated.
- [ ] Tests or manual verification notes cover the change.
- [ ] No audio files, archives, generated reports, or copyrighted content are
      included.
- [ ] No personal paths, private metadata, credentials, or machine-specific
      files are included.
- [ ] Documentation and changelog entries are updated when needed.
