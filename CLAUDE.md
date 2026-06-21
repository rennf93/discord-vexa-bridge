# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## Project overview

`discord-vexa-bridge` brings Discord voice calls into [Vexa](https://github.com/Vexa-ai/vexa)
as speaker-tagged transcripts. A bot joins a voice channel, receives **each speaker as a
separate audio stream**, transcribes every utterance on Vexa's Whisper worker, and writes the
rows straight into Vexa's Postgres — so a Discord call appears like any other meeting on the
dashboard / API / MCP.

The hard part: since March 2026 Discord enforces **E2EE on all voice via the DAVE protocol**.
Off-the-shelf libraries can't decrypt it, so this repo implements the DAVE **receive** path
itself (voice-gateway handshake, MLS group join, per-sender frame decryption via `dave.py`).

**Python**: pinned to **3.11** — it uses the stdlib `audioop` module, removed in 3.13. Do not
bump the runtime or base image past 3.11.
**Package manager**: uv. **Not** a published package (`package = false`) — it's a runnable bot.

## Layout

- `bot.py` — control plane: slash commands (`/join`, `/leave`), the silence-gap segmenter,
  the transcription call, and the Postgres writes.
- `dave_voice/` — the voice-receive internals:
  - `discord_protocol.py` — py-cord `VoiceProtocol` shim that captures the handshake creds
    (token / endpoint / session_id); the real voice WS+UDP is owned by `DAVEVoiceClient`.
  - `voice_ws.py` / `voice_client.py` — voice gateway v8 + the receive pipeline.
  - `transport.py` — rtpsize transport decrypt (AES-256-GCM / XChaCha20).
  - `mls.py` — MLS group join + per-sender key ratchets.
  - `rtp.py`, `udp_receiver.py`, `ip_discovery.py`, `opcodes.py` — RTP/UDP plumbing.
  - `opus_decode.py` — per-SSRC Opus → 48 kHz PCM (decode with `fec=False`).
- `tests/` — unit tests (pytest, asyncio_mode=auto). No network or live Discord required.

## Pipeline (data flow)

DAVE/E2EE voice → voice gateway v8 + MLS join → transport decrypt → DAVE frame decrypt →
Opus decode → 48 kHz PCM → downsample to 16 kHz → silence-gap segmenter → POST to Vexa's
Whisper worker → INSERT into `meetings` / `meeting_sessions` / `transcriptions` (platform =
`discord`).

## Commands

```bash
make install     # uv sync --extra dev
make test        # pytest
make lint        # ruff check + format check
make fix         # ruff auto-fix + format
make typecheck   # mypy (must stay clean)
make security    # bandit
make check-all   # lint + typecheck + security + test
make pre-commit  # run all pre-commit hooks
make build       # build the Docker image
```

CI (`.github/workflows/ci.yml`) runs `pre-commit run --all-files` (ruff, ruff-format, mypy,
bandit) and the pytest suite on Python 3.11. Keep all of these green.

## Conventions

- Match the surrounding style. The explanatory comments in `dave_voice/` document a tricky,
  Discord-controlled protocol — keep them accurate; don't strip them.
- `mypy bot.py dave_voice` is currently clean (0 errors) and gated in CI — keep it that way.
  Prefer narrowing/guards over `# type: ignore`.
- Never commit secrets. `DISCORD_TOKEN`, `DATABASE_URL`, and `.env` are runtime config and are
  gitignored.
- DAVE is a moving target; protocol bumps may require updating the decrypt path / `dave.py`.
