# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial release: a Discord → Vexa voice-transcription bridge.
- **DAVE (E2EE) voice receive** implemented from scratch in Python: voice gateway v8 handshake,
  MLS group join with per-sender key ratchets, transport decryption
  (AES-256-GCM / XChaCha20, rtpsize), and DAVE frame decryption via `dave.py` (libdave).
- Per-user capture (native diarization), Opus decode, downsample to 16 kHz, silence-gap
  segmentation, and transcription via Vexa's Whisper worker.
- Writes `meetings` / `meeting_sessions` / `transcriptions` rows (`platform = "discord"`) so calls
  appear in the Vexa dashboard / API / MCP like Zoom and Meet.
- `/join` and `/leave` slash commands.
- Lazy, retrying DB pool and transcription POST for resilience to stack restarts.
