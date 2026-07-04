# Security Policy

## Local-first design

Music Manager is a local-first application. The current scan workflow reads a
user-selected folder and writes a CSV inventory locally. Analysis reads that
inventory and writes local reports without reopening the referenced music
files. Neither workflow uploads audio files or report data, and neither
workflow renames, moves, copies, deletes, or retags music files.

MusicBrainz integration, staging, retagging, organization, dashboards, and
inbox automation are planned features, not current capabilities. Future network
access must require explicit opt-in. Future write operations must be limited to
a separate staging library, show a reviewable plan, and require explicit user
approval.

Library reports can still contain sensitive information, including personal
directory names, filenames, and listening-library metadata. Do not upload
private reports to public issues, pull requests, paste services, or shared
drives without reviewing and sanitizing them.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository when it is
enabled.

If private reporting is unavailable, open a minimal public issue that describes
the affected component and general impact without exploit details, private
paths, library reports, audio files, credentials, or other sensitive data. A
maintainer can then arrange an appropriate private channel.

Please include the affected version and enough non-sensitive information to
reproduce or assess the issue.
