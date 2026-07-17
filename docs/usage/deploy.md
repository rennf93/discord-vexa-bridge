# Deploy

The bridge runs as one more Docker Compose service on your existing Vexa network, next to
`postgres` and `transcription-worker`. It is **self-hosted, bring-your-own-bot software** —
there is no hosted service and no shared bot. See the README's "Who runs this" section for the
token-handling model: your `DISCORD_TOKEN` is runtime config that never leaves your own
infrastructure.

## Prerequisites

- A running **[Vexa](https://github.com/Vexa-ai/vexa)** stack (Postgres + the transcription worker,
  on a shared Docker network).
- A **Discord bot** (free — create one in the
  [Discord Developer Portal](https://discord.com/developers/applications)). See the README
  Quick start Step 1 for the bot setup (intents: **Server Members**; scopes: `bot` +
  `applications.commands`; permissions: **View Channels**, **Connect**).
- Vexa must accept `discord` as a platform — see [Vexa version targeting](vexa-version-targeting.md).
- Docker / Docker Compose.

## Compose service

Add this service to your Vexa `docker-compose.yaml` (same network as `postgres` and
`transcription-worker`). The canonical source is [`compose-snippet.yml`](https://github.com/rennf93/discord-vexa-bridge/blob/master/compose-snippet.yml)
in the repo:

```yaml
  discord-vexa-bridge:
    image: ghcr.io/rennf93/discord-vexa-bridge:latest
    # Or build from a local checkout instead of the prebuilt image:
    # build:
    #   context: ./discord-vexa-bridge
    environment:
      DISCORD_TOKEN: "${DISCORD_TOKEN}"     # from a .env file — do NOT commit the token
      DATABASE_URL: "${DATABASE_URL:-postgresql://postgres:CHANGE_ME@postgres:5432/vexa}"
      TRANSCRIBE_URL: http://transcription-worker:8000/v1/audio/transcriptions
      VEXA_USER_ID: "1"          # the Vexa user id that owns these meetings
      LANGUAGE: ""               # "" = autodetect (leave blank for mixed it/es/en)
      SILENCE_MS: "800"          # gap (ms) that ends an utterance
    depends_on: [postgres, transcription-worker]
    volumes:
      # Durable segment queue — see "The /data/pending volume" below.
      - discord-pending:/data/pending
    networks: [vexa]
    restart: unless-stopped

# And declare the volume at the top level of your docker-compose.yaml:
#   volumes:
#     discord-pending:
```

Then:

```bash
docker compose up -d discord-vexa-bridge
```

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | yes | - | Discord bot token. |
| `DATABASE_URL` | yes | - | Vexa Postgres DSN, e.g. `postgresql://postgres:pw@postgres:5432/vexa`. |
| `VEXA_USER_ID` | yes | - | Vexa user id that owns the created meetings. |
| `TRANSCRIBE_URL` | no | `http://transcription-worker:8000/v1/audio/transcriptions` | Vexa Whisper endpoint. |
| `LANGUAGE` | no | `""` (autodetect) | Force a transcription language code (e.g. `en`, `es`). |
| `SILENCE_MS` | no | `800` | Silence gap (ms) that ends an utterance. |
| `MIN_UTTERANCE_MS` | no | `400` | Drop utterances shorter than this. |

See [Configuration](config.md) for the full list including the durable-queue knobs.

## The `/data/pending` volume — do not skip this

!!! warning "Data loss without a mounted volume"
    The durable segment queue lives at `/data/pending`. The compose snippet mounts the named
    volume `discord-pending` there. **If you do not mount a volume**, `/data/pending` lives in the
    container's ephemeral writable layer and **`docker compose up -d` that recreates the container
    (e.g. pulling a new image) silently wipes every pending segment** — losing captured audio on
    the next deploy. This lost real data once (fixed in 0.4.1; see
    [CHANGELOG](https://github.com/rennf93/discord-vexa-bridge/blob/master/CHANGELOG.md)).

Always declare the top-level `volumes:` entry:

```yaml
volumes:
  discord-pending:
```

## Use it

In Discord: **`/join`** (joins your current voice channel and starts transcribing) and **`/leave`**
(stops and finalizes the meeting). Transcripts appear in your Vexa dashboard / API / MCP.

## Cross-building for a NAS (amd64)

If you deploy to an amd64 NAS from an Apple Silicon host, build for the target platform:

```bash
docker buildx build --platform linux/amd64 -t ghcr.io/rennf93/discord-vexa-bridge:latest --push .
```

See [Troubleshooting](troubleshooting.md) for more.
