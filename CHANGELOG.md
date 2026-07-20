# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.6.0] - 2026-07-20

### Added
- **Vexa 0.12.x compatibility** (verified against v0.12.15). The direct-Postgres contract survives the 0.12 rewrite unchanged (`meetings.platform_specific_id` String, `transcriptions.segment_id` nullable String, `meeting_sessions` identical), and 0.12's read API serves `discord` rows — only bot endpoints reject the platform, which this bridge never calls. Deployment notes for 0.12.x (reconcile grace, token-open transcription unit) are in `docs/usage/vexa-version-targeting.md`.

### Fixed
- **`/join` retires stale non-terminal meeting rows for the channel before inserting.** A crash mid-call used to leave the row `active` forever; under Vexa 0.12's new unique partial index (one non-terminal meeting per user/platform/channel) that made the next `/join` of the same channel fail with an unhandled `UniqueViolationError` and leak the voice connection. The already-queued segments of the retired row still drain into the DB.
- **Each transcription insert now bumps `meetings.updated_at`.** Vexa 0.12's reconcile sweep reaps non-terminal meetings by `updated_at` staleness (bridge rows have no bot container it could probe instead); without the heartbeat a live call was force-completed after ~5 minutes and downstream consumers (e.g. obsidian-vexa-bridge) summarized partial transcripts. Pair with a raised `RECONCILE_ACTIVE_GRACE_S` to also survive long silences.

## [0.5.1] - 2026-07-07

### Added
- CLA assistant workflow + CLA.md, a documentation issue template, and a `scripts/bump_version.py` changelog/version helper. Parity with `obsidian-vexa-bridge`.

## [0.5.0] - 2026-07-07

### Removed
- **The `summarizer/` package has moved to its own repo, `obsidian-vexa-bridge`** (github.com/rennf93/obsidian-vexa-bridge) — the Vexa → Obsidian sink adapter, the out-side of the in/out adapter pair around Vexa. The bridge is now Discord-in only. The bridge Docker image never copied `summarizer/` (it was a Mac-side `uv sync --extra summarizer` runner), so the adapter image and runtime behavior are unchanged. The `summarizer` optional-dependency extra and its mypy/bandit CI targets are dropped. 58 tests pass (was 109); mypy/ruff/bandit clean on `bot.py dave_voice`.

## [0.4.1] - 2026-07-07

### Fixed
- **The drainer no longer blocks the whole queue on a silence clip.** `drain_once`
  treated an empty `transcribe()` result as "worker busy/down" and retried the same
  FIFO head file with backoff. But the Whisper worker returns 200 OK with empty text
  when VAD strips a clip (no speech) — a legitimate result, not a failure. So a single
  silence segment at the head of the queue blocked every segment behind it indefinitely.
  Live symptom: a real Discord call spilled 367 segments (both speakers captured
  correctly — the DAVE receive path was fine) but only the ~9 drained in realtime before
  `/leave` ever reached Vexa; the meeting stayed `active` because the drainer was stuck
  retrying one 0.5s silence clip for hours. `transcribe` now returns `None` only when the
  worker is genuinely unavailable (non-200 / connection error / exception → retry); a
  200-OK response returns the text (possibly empty), and `drain_once` deletes the clip
  and advances whether or not there was speech. Silence can no longer block the queue.
- **The pending queue is actually durable across deploys.** The compose snippet never
  mapped `/data/pending` to a volume, so the "durable" queue from 0.2.0 lived in the
  container's ephemeral writable layer and any `compose up -d` that recreated the
  container (e.g. pulling a new image) silently wiped every spilled segment — losing
  captured audio on the next deploy. This lost real data once. `compose-snippet.yml`
  now mounts `discord-pending:/data/pending` (with a top-level volume declaration).

### Verified
- End-to-end on a real two-speaker Discord call: both speakers (Renzo + Dollylogon)
  captured via DAVE, spilled to the durable queue, drained through the worker, inserted,
  meeting auto-completed when the queue emptied, and the summarizer wrote an Obsidian
  note with both participants. 109 tests pass; mypy/ruff/bandit clean.

## [0.4.0] - 2026-07-07

### Fixed
- **The Obsidian sink actually writes notes.** Two silent-failure bugs in
  `summarizer/obsidian.py` had the summarizer report success while writing nothing to
  the vault:
  - `note_path` used `(HH:MM)`; Obsidian filenames cannot contain `:`, so `vault-as-mcp`
    rejected the path with a tool-execution error. Now `(HH-MM)`.
  - `create_note` checked only the top-level JSON-RPC `error`, not `result.isError`, so
    HTTP-200 + `isError:true` tool failures were swallowed. Now honors both shapes;
    "File already exists" still trips the idempotent crash-recovery backstop.
- **Live Vexa API shapes.** `summarizer/vexa.py` now parses what the api-gateway actually
  returns: ISO-8601 `start_time`/`end_time` strings (it assumed epoch seconds) and the
  `{"segments":[{start,end,text,speaker}]}` transcript shape (it expected
  `{"transcripts":[...]}`). Verified against the running NAS stack.

### Changed
- **LLM routed through the local ollama proxy.** Defaults switched to
  `openai/glm-5.2:cloud` at `http://localhost:11434/v1` — the proxy's OpenAI endpoint
  serves the `:cloud` models with no auth. Plain `ollama/*` ids hit `/api/generate`, and
  the local raw models have no template (`does not support chat`); only the `:cloud`
  models work. Updated `summarizer/setup_env.sh` and the launchd plist; also fixed a
  quoting syntax error in `setup_env.sh`'s NAS diagnostic block.

### Added
- `summarizer/setup_env.sh` and `summarizer/mint_token.sh` helpers — secret-free; they
  pull the Vexa admin token (SSH, NAS `.env`) and the Obsidian MCP bearer (local
  `vault-as-mcp` plugin config) at runtime and write a gitignored `.env.summarizer.local`.

## [0.3.0] - 2026-07-06

### Added
- **Meeting summarizer → Obsidian.** A new `summarizer/` package (run as `python -m summarizer`
  on a Mac launchd timer) reads completed meetings from Vexa, summarizes each via LiteLLM, and
  writes a structured note (TL;DR, key/talking points, decisions, action items with `@owner`,
  open questions, full breakdown, optional transcript) to the local Obsidian vault through the
  `vault-as-mcp` plugin. Also optional writes the summary to the Vexa meeting `notes` field.
- **Provider-agnostic LLM.** `AI_MODEL=provider/model` + `AI_API_KEY` + `AI_BASE_URL`, mirroring
  Vexa's dashboard convention — Anthropic Sonnet 5 by default; `ollama/llama3` +
  `AI_BASE_URL=http://localhost:11434` for self-hosted; any OpenAI-compatible endpoint (vLLM,
  Groq, OpenRouter). `litellm` is a new `summarizer` extra (lazy-imported, so the adapter image
  and the test suite don't depend on it).
- **Idempotent, crash-safe.** Local `state.json` is the source of truth (Vexa's PATCH can't
  write arbitrary metadata); `create_note`'s fail-if-exists is the crash-recovery backstop.
  Meetings below `MIN_TRANSCRIPT_SECONDS` are skipped-not-summarized; a meeting that fails 5
  passes is poisoned and skipped until `state.json` is cleared. `DRY_RUN` runs the full
  pipeline (including the LLM call) without writing or marking.
- `summarizer/launchd/com.rennf.vexa-summarizer.plist` template for the Mac timer.
- `summarizer` added to the mypy + bandit CI gates.

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
