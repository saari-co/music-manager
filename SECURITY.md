# Security Policy

## Local-first design

Music Manager is a local-first application. Version 0.1.0 reads a user-selected
folder and writes a CSV report locally. It does not upload audio files or report
data.

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
