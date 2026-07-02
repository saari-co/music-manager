# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-02

### Added

- Read-only recursive scanner for MP3, FLAC, M4A, AAC, and WAV files.
- CSV report containing file details, audio metadata, bitrate, duration, and
  likely loose-track status.
- ZIP archive detection without extraction.
- Per-file and per-directory error handling that allows scans to continue.
- Terminal summary with audio, archive, loose-track, and error counts.
