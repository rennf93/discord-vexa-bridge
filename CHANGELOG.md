# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-06

### Fixed
- **No more lost audio when the transcription worker is busy.** Segments now spill to a
  durable on-disk queue and a single background drainer replays them one at a time,
  matching the worker's single-concurrency. Previously a 503 (or a bot restart) discarded
  the PCM, so a long call could yield only a handful of transcript rows.
- Transcribe timeout raised 120s → 600s (`TRANSCRIBE_TIMEOUT`): the CPU Whisper worker
  holds a connection for minutes per clip; 120s was killing even accepted jobs.

### Changed
- `/leave` marks the meeting `completed` only after the pending queue has drained (not
  inline), so anything reacting to `completed` (summaries, webhooks) sees every utterance.

### Added
- `PENDING_DIR` env (default `/data/pending`) and a `discord-pending` compose volume for
  the durable queue — survives 503s and bot restarts; the drainer recovers crash-left
  segments on boot.

## [0.1.0] - 2026-06-21

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
