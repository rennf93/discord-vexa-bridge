# Configuration

All configuration is via environment variables — there is no config file. The bot reads them at
startup in `bot.py`.

## Required

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token (from the Developer Portal). Never commit it; keep it in a gitignored `.env`. |
| `DATABASE_URL` | Vexa Postgres DSN, e.g. `postgresql://postgres:pw@postgres:5432/vexa`. |
| `VEXA_USER_ID` | Vexa user id (integer) that owns the meetings created by the bridge. |

## Transcription / segmentation

| Variable | Default | Description |
|---|---|---|
| `TRANSCRIBE_URL` | `http://transcription-worker:8000/v1/audio/transcriptions` | Vexa Whisper endpoint. |
| `LANGUAGE` | `""` (autodetect) | Force a transcription language code (e.g. `en`, `es`). |
| `SILENCE_MS` | `800` | Silence gap (ms) that ends an utterance. Discord only sends voice packets while a user is transmitting, so a gap with no packets ends an utterance. |
| `MIN_UTTERANCE_MS` | `400` | Drop utterances shorter than this (blips / mic bumps). |
| `TRANSCRIBE_TIMEOUT` | `600` | Per-transcribe timeout (seconds). The CPU Whisper worker holds a connection for minutes per clip; 120s was killing even accepted jobs. |

## Durable pending queue

Segments spill to disk first and a single background drainer replays them, so a 503 or a bot
restart never loses audio. The queue survives crashes and recovers crash-left segments on boot.

| Variable | Default | Description |
|---|---|---|
| `PENDING_DIR` | `/data/pending` | Where spilled segments live. **Must be a mounted volume** — see [Deploy](deploy.md#the-datapending-volume-do-not-skip-this). |
| `DRAIN_IDLE_SLEEP` | `5` | Seconds the drainer waits before rescanning an empty queue. |
| `DRAIN_BACKOFF` | `10` | Seconds the drainer pauses after a failed transcribe attempt (worker busy/down). |

!!! warning "Mount `PENDING_DIR` to a volume"
    Without a mounted volume, the durable queue lives in the container's ephemeral writable
    layer and `docker compose up -d` that recreates the container wipes every pending segment.
    See [Deploy](deploy.md#the-datapending-volume-do-not-skip-this).

## Slash commands

| Command | Description |
|---|---|
| `/join` | Joins your current voice channel and starts transcribing. |
| `/leave` | Stops transcribing, flushes buffered audio, and marks the meeting for completion once the pending queue drains. |

`/leave` does **not** mark the meeting `completed` inline — the slow worker may still be draining
spilled segments, and anything reacting to `completed` (summaries, webhooks) needs every
utterance. The drainer flips the status once this meeting's pending queue is empty.