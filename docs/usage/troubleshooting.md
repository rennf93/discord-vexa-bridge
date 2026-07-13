# Troubleshooting

## Pool-init deferred startup race

Symptom: `/join` fails with an `AttributeError` on `pool`, or the log shows
`pool init deferred (will retry on first /join)`.

The Discord gateway can become ready before Postgres accepts connections (Compose
`depends_on` doesn't wait for DB readiness). `bot.py` creates the asyncpg pool lazily on first
use (`ensure_pool()`), guarded by an async lock, so this is expected and self-heals: the first
`/join` after Postgres is up creates the pool. If the DB is genuinely unreachable/misconfigured,
`ensure_pool()` surfaces a clear asyncpg error.

Fix: confirm Postgres is healthy (`pg_isready`), and that `DATABASE_URL` points at it.

## Cross-building for an amd64 NAS

If you deploy to an amd64 NAS (e.g. the UGREEN NAS) from an Apple Silicon host, build for the
target platform explicitly:

```bash
docker buildx build --platform linux/amd64 -t ghcr.io/rennf93/discord-vexa-bridge:latest --push .
```

Building without `--platform linux/amd64` on an arm64 host produces an arm64 image the NAS can't
run.

## Python 3.11 pin (audioop)

The bot uses the stdlib `audioop` module, **removed in Python 3.13**. The runtime and base image
are pinned to **3.11** — do not bump past 3.11. If you build a custom image and accidentally
move to 3.12+/3.13, `import audioop` fails at startup.

## Transcription latency

Transcription latency = your Whisper worker. On CPU with `large-v3-turbo` a short utterance can
take tens of seconds. Drop the worker's `MODEL_SIZE` to `small`/`medium`, or give it a GPU, for
snappier results. Segments are durable — they spill to `/data/pending` and the drainer replays
them, so a slow worker delays rows but never drops them.

## Pending queue wiped on deploy

If every pending segment disappears on `docker compose up -d`, you did not mount a volume at
`/data/pending`. See [Deploy](deploy.md#the-datapending-volume-do-not-skip-this) — the
named volume `discord-pending` is required, with a top-level `volumes:` declaration.

## Frames failing to decrypt at stream start

A handful of frames fail to decrypt at stream start / epoch edges — this is harmless; the
successful frames produce accurate transcripts. libdave logs per-frame decrypt failures through
the `dave` logger, which the bot silences (`logging.getLogger("dave").setLevel(logging.CRITICAL)`)
because the noise is expected.

## Vexa returns 422 for Discord transcripts

Vexa's read API rejects the `discord` platform until it's in the platform enum. See
[Vexa version targeting](vexa-version-targeting.md) and README Step 2.