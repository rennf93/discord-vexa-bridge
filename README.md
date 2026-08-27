# discord-vexa-bridge

[![CI](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/ci.yml)
[![Release](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/release.yml/badge.svg)](https://github.com/rennf93/discord-vexa-bridge/actions/workflows/release.yml)
[![License: AGPL-3.0-or-later](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-mkdocs%20material-blue.svg)](https://rennf93.github.io/discord-vexa-bridge/)
[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/release/python-3110/)

Bring **Discord voice calls into [Vexa](https://github.com/Vexa-ai/vexa)** as transcripts — speaker-tagged, per user, alongside your Zoom/Meet meetings, and readable by your AI agents over Vexa's MCP server.

A small bot joins a Discord voice channel, receives **each speaker as a separate audio stream** (native diarization — no clustering), transcribes every utterance on the Whisper worker Vexa already runs, and writes the rows straight into Vexa's Postgres. On the dashboard / API / MCP, a Discord call then looks just like any other meeting.

> **The hard part — and why this exists:** since March 2026, Discord enforces **E2EE on all voice via the DAVE protocol**. Off-the-shelf Python/JS voice libraries can't decrypt it ([py-cord #3139](https://github.com/Pycord-Development/pycord/issues/3139), [discord.js voice](https://github.com/discordjs/discord.js/issues/11419)). This bridge implements the DAVE **receive** path — the voice-gateway handshake, the MLS group join, and per-sender frame decryption (via [`dave.py`](https://pypi.org/project/dave.py/), the libdave binding) — so per-user capture works **today**.

> **Which Vexa version?** Bridge **0.6.0+** runs against **Vexa 0.10.x and 0.12.x** (verified on v0.12.15). Vexa 0.12's read API serves `discord` rows fine; only its *bot* endpoints reject the platform, and this bridge writes directly to Postgres, bypassing them. On 0.12.x you must raise `RECONCILE_ACTIVE_GRACE_S` on meeting-api and keep the transcription unit token-open — see [Which Vexa version?](docs/usage/vexa-version-targeting.md) for the full version map and the 0.12 deployment notes. The schema is still Vexa-internal; the sealed external-ingest contract remains tracked in [Vexa #463](https://github.com/Vexa-ai/vexa/issues/463).

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

The downstream is exactly what Vexa's own Zoom/Meet bots use, so Discord calls appear in the dashboard, the REST API, and the MCP server without special handling — **provided Vexa knows the `discord` platform** (see [Step 2](#step-2---teach-vexa-the-discord-platform)).

---

## Who runs this, and where your token lives

**This is self-hosted, bring-your-own-bot software. There is no hosted service and no shared bot.** Anyone who wants Discord → Vexa transcripts — the maintainer included — runs **their own copy** of this bridge next to **their own Vexa stack**, using **their own Discord bot**. Read this before you worry about your token: it never leaves your own infrastructure.

- **You create your own Discord bot and use its token.** There is no "add our bot to your server" invite link to click. Each operator makes a bot in the [Discord Developer Portal](https://discord.com/developers/applications) ([Step 1](#step-1---create-the-discord-bot)); the token it gives you is yours alone.
- **Your token is runtime configuration — it is never baked into the image and never lives in this repo.** You put it in a gitignored `.env` on your own server, Docker Compose passes it to the container as an environment variable, and `bot.py` reads it from `os.environ["DISCORD_TOKEN"]` at startup. The whole lifecycle stays on your machine:

  ```
  Discord Developer Portal
    -> your .env file        (gitignored, stays on your server)
    -> docker compose environment:
    -> os.environ["DISCORD_TOKEN"]   (read at runtime, inside your container)
  ```

- **The published images contain zero secrets.** `ghcr.io/rennf93/discord-vexa-bridge` and `docker.io/renzof93/discord-vexa-bridge` are generic binaries — they are safe to be public precisely *because* the bot token and the database password are supplied per-deployment at runtime, not built in.
- **Nobody handles anyone else's token.** Your token (and your DB password) are never sent to the maintainer or any third party. At runtime the bot talks only to Discord, to **your** Vexa Postgres, and to **your** Vexa Whisper worker — nothing else.
- **One bot = one identity = one Vexa.** This bridge is single-bot / single-Vexa by design. Offering it as a shared, multi-server hosted product would be a different (multi-tenant) architecture and is **not** what this repo does.

> **The maintainer is just "operator #1."** To run the bot on their own servers, the maintainer follows the exact same [Quick start](#quick-start) below with their own bot token. There is no privileged, central, or hosted instance — everyone self-hosts identically.

---

## Requirements

- A running **[Vexa](https://github.com/Vexa-ai/vexa)** stack (Postgres + the transcription worker, on a shared Docker network).
- A **Discord bot** (free — created below).
- Docker / Docker Compose to run this bridge as one more service on Vexa's network.
- Vexa must accept `discord` as a platform ([Step 2](#step-2---teach-vexa-the-discord-platform)).

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

Vexa validates the meeting platform against a fixed enum (`google_meet`, `zoom`, `teams`, `browser_session`). Until `discord` is in it, Vexa's read API returns **422** for Discord transcripts (the data is written fine, but the dashboard/MCP won't serve it).

Add `DISCORD = "discord"` to `Platform` in `services/meeting-api/meeting_api/schemas.py` and rebuild your `meeting-api` + `api-gateway` images. A ready-made change (enum + URL handling + concurrency exclusion + dashboard icon) is here: **[the `discord` platform patch](https://github.com/rennf93/vexa/tree/add-discord-platform)** - ideally land it upstream so future Vexa releases include it.

### Step 3 - Deploy the bridge

Add this service to your Vexa `docker-compose.yaml` (same network as `postgres` and `transcription-worker`). See [`compose-snippet.yml`](compose-snippet.yml):

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

In Discord: **`/join`** (joins your current voice channel and starts transcribing) and **`/leave`** (stops and finalizes the meeting). Transcripts appear in your Vexa dashboard / API / MCP.

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
| `COMPLETION_WEBHOOK_URL` | no | unset (feature off) | Receiver URL to POST a `meeting.completed` webhook to once a meeting's pending queue drains and its row flips to `completed`. |
| `COMPLETION_WEBHOOK_SECRET` | required if `COMPLETION_WEBHOOK_URL` is set | - | HMAC secret used to sign the webhook body; the bridge fails fast at startup if the URL is set without it. |

---

## Completion webhook

Vexa's own `meeting.completed` webhook never fires for Discord meetings; this bridge writes them straight into Vexa's Postgres, bypassing the code path that would fire it. Set `COMPLETION_WEBHOOK_URL` (and `COMPLETION_WEBHOOK_SECRET`) to have the bridge emit a `meeting.completed` event once a meeting is marked `completed` (its pending transcription queue has fully drained, see `_finalize_completed` in `bot.py`): `event_id`, `event_type`, `api_version`, `created_at`, and a `data.meeting` object carrying `id`, `platform`, `native_meeting_id`, `status`, `completion_reason`, `start_time`, and `end_time`, plus a `source` marker identifying this bridge. This is not Vexa's full `webhook.v1` `data.meeting` object: Vexa's also carries `user_id`, `constructed_meeting_url`, `failure_stage`, `data`, `created_at`, and `updated_at`, and this bridge's `event_id` is hashed from bridge-specific input, not Vexa's. The signing scheme matches Vexa's, though: the request is `POST` `Content-Type: application/json` with `X-Webhook-Timestamp`, `X-Webhook-Signature: sha256=<hmac_sha256(secret, timestamp + "." + raw_body)>`, and `Authorization: Bearer <secret>` headers. Delivery is fire-and-forget (it never blocks the drainer) and retries a few times on failure before giving up. `event_id` is deterministic per meeting (`evt_<sha256(...)[:32]>`), so a redelivered event dedups on the receiving end. [`obsidian-vexa-bridge`](https://github.com/rennf93/obsidian-vexa-bridge) is the reference receiver: it accepts this envelope shape and processes the meeting on the event instead of polling Vexa's API.

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

See [CONTRIBUTING.md](CONTRIBUTING.md). The voice-receive internals live in `dave_voice/`; `bot.py` is the control plane (slash commands + the transcription/DB pipeline).

## Documentation

Full docs — deployment, configuration, troubleshooting, and the DAVE receive architecture — are at **<https://rennf93.github.io/discord-vexa-bridge/>**.

## License

This project is **dual-licensed**:

- **Open-source use** under the **GNU AGPL-3.0-or-later** ([LICENSE](LICENSE)) — free, with the network-use source-disclosure obligation of AGPL §13.
- A **commercial license** is available for those who cannot or do not wish to comply with the AGPL (e.g. embedding in a closed SaaS / proprietary product) — see [LICENSE-COMMERCIAL.md](LICENSE-COMMERCIAL.md).

(c) Renzo Franceschini. Not affiliated with Discord or Vexa.
