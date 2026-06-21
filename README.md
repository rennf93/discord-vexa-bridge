# discord-vexa-bridge

[![CI](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/ci.yml)
[![Release](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/release.yml/badge.svg)](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)

Bring **Discord voice calls into [Vexa](https://github.com/Vexa-ai/vexa)** as transcripts —
speaker-tagged, per user, alongside your Zoom/Meet meetings, and readable by your AI agents over Vexa's MCP server.

A small bot joins a Discord voice channel, receives **each speaker as a separate audio stream**
(native diarization — no clustering), transcribes every utterance on the Whisper worker Vexa
already runs, and writes the rows straight into Vexa's Postgres. On the dashboard / API / MCP, a
Discord call then looks just like any other meeting.

> **The hard part — and why this exists:** since March 2026, Discord enforces **E2EE on all voice
> via the DAVE protocol**. Off-the-shelf Python/JS voice libraries can't decrypt it
> ([py-cord #3139](https://github.com/Pycord-Development/pycord/issues/3139),
> [discord.js voice](https://github.com/discordjs/discord.js/issues/11419)). This bridge implements
> the DAVE **receive** path — the voice-gateway handshake, the MLS group join, and per-sender frame
> decryption (via [`dave.py`](https://pypi.org/project/dave.py/), the libdave binding) — so per-user
> capture works **today**.

---

## How it works

```
Discord voice (DAVE/E2EE)
  -> voice gateway v8 + MLS group join (per-sender key ratchets)
  -> transport decrypt (AES-256-GCM / XChaCha20, rtpsize)
  -> DAVE frame decrypt (libdave via dave.py)
  -> Opus decode -> 48 kHz PCM -> downsample to 16 kHz
  -> silence-gap segmenter (one utterance per speaker)
  -> POST to Vexa's transcription worker (Whisper)
  -> INSERT into meetings / meeting_sessions / transcriptions  (platform = "discord")
```

The downstream is exactly what Vexa's own Zoom/Meet bots use, so Discord calls appear in the
dashboard, the REST API, and the MCP server without special handling — **provided Vexa knows the
`discord` platform** (see [Step 2](#step-2--teach-vexa-the-discord-platform)).

---

## Requirements

- A running **[Vexa](https://github.com/Vexa-ai/vexa)** stack (Postgres + the transcription worker, on a shared Docker network).
- A **Discord bot** (free — created below).
- Docker / Docker Compose to run this bridge as one more service on Vexa's network.
- Vexa must accept `discord` as a platform ([Step 2](#step-2--teach-vexa-the-discord-platform)).

---

## Quick start

### Step 1 - Create the Discord bot

1. Go to the **[Discord Developer Portal](https://discord.com/developers/applications)** -> **New Application** -> name it (e.g. *Vexa Notes*).
2. Open the **Bot** tab -> **Reset Token** -> **Copy**. This is your `DISCORD_TOKEN` - keep it secret, never commit it.
3. Still on the **Bot** tab, under **Privileged Gateway Intents**, enable:
   - **Server Members Intent** - for reliable speaker display names.
   - *(Voice States is a standard intent and is enabled by the bridge automatically. Message Content is **not** needed - the bridge uses slash commands.)*
4. **Invite the bot to your server.** Go to **OAuth2 -> URL Generator**:
   - **Scopes:** `bot` and `applications.commands`
   - **Bot Permissions:** `View Channels`, `Connect`
   - Open the generated URL, pick your server, **Authorize**.
5. After deploying (Step 3), join a voice channel and run **`/join`**; **`/leave`** stops it.

### Step 2 - Teach Vexa the `discord` platform

Vexa validates the meeting platform against a fixed enum (`google_meet`, `zoom`, `teams`,
`browser_session`). Until `discord` is in it, Vexa's read API returns **422** for Discord
transcripts (the data is written fine, but the dashboard/MCP won't serve it).

Add `DISCORD = "discord"` to `Platform` in `services/meeting-api/meeting_api/schemas.py` and rebuild
your `meeting-api` + `api-gateway` images. A ready-made change (enum + URL handling + concurrency
exclusion + dashboard icon) is here: **[the `discord` platform patch](https://github.com/rennf93/vexa/tree/add-discord-platform)** -
ideally land it upstream so future Vexa releases include it.

### Step 3 - Deploy the bridge

Add this service to your Vexa `docker-compose.yaml` (same network as `postgres` and
`transcription-worker`). See [`compose-snippet.yml`](compose-snippet.yml):

```yaml
  discord-vexa-bridge:
    image: ghcr.io/rennf93/discord-vexa-bridge:latest   # or: build: ./discord-vexa-bridge
    environment:
      DISCORD_TOKEN: "${DISCORD_TOKEN}"                  # from Step 1 - keep it in a .env, never commit
      DATABASE_URL: postgresql://postgres:CHANGE_ME@postgres:5432/vexa
      TRANSCRIBE_URL: http://transcription-worker:8000/v1/audio/transcriptions
      VEXA_USER_ID: "1"                                  # the Vexa user id that owns these meetings
      LANGUAGE: ""                                       # "" = autodetect
      SILENCE_MS: "800"                                  # gap (ms) that ends an utterance
    depends_on: [postgres, transcription-worker]
    networks: [vexa]
    restart: unless-stopped
```

```bash
docker compose up -d discord-vexa-bridge
```

### Step 4 - Use it

In Discord: **`/join`** (joins your current voice channel and starts transcribing) and **`/leave`**
(stops and finalizes the meeting). Transcripts appear in your Vexa dashboard / API / MCP.

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | - | Discord bot token (Step 1). |
| `DATABASE_URL` | yes | - | Vexa Postgres DSN, e.g. `postgresql://postgres:pw@postgres:5432/vexa`. |
| `VEXA_USER_ID` | yes | - | Vexa user id that owns the created meetings. |
| `TRANSCRIBE_URL` | no | `http://transcription-worker:8000/v1/audio/transcriptions` | Vexa Whisper endpoint. |
| `LANGUAGE` | no | `""` (autodetect) | Force a transcription language code (e.g. `en`, `es`). |
| `SILENCE_MS` | no | `800` | Silence gap (ms) that ends an utterance. |
| `MIN_UTTERANCE_MS` | no | `400` | Drop utterances shorter than this. |

---

## Notes & limitations

- **Python is pinned to 3.11** - it uses the stdlib `audioop` module, removed in 3.13.
- **Transcription latency = your Whisper worker.** On CPU with `large-v3-turbo` a short utterance can take tens of seconds; drop the worker's `MODEL_SIZE` to `small`/`medium`, or give it a GPU, for snappier results.
- **DAVE is a moving, Discord-controlled protocol.** The decrypt path tracks the published spec via `dave.py`/libdave; protocol bumps may need updates.
- A handful of frames fail to decrypt at stream start / epoch edges - harmless; the successful frames produce accurate transcripts.
- The Vexa **dashboard meeting-detail view** is built around its bot pipeline; the transcript reads fine via API/MCP, but that page may need the Step 2 patch to render cleanly.

---

## Development

```bash
uv sync --extra dev     # install runtime + dev deps
make test               # run the unit suite
make lint               # ruff lint + format check
make fix                # auto-fix
make build              # build the Docker image
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The voice-receive internals live in `dave_voice/`; `bot.py`
is the control plane (slash commands + the transcription/DB pipeline).

## License

[MIT](LICENSE) (c) Renzo Franceschini. Not affiliated with Discord or Vexa.
