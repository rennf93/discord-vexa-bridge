# discord-vexa-bridge

Bring **Discord voice calls into [Vexa](https://github.com/Vexa-ai/vexa)** as speaker-tagged
transcripts — alongside your Zoom/Meet meetings, readable by your AI agents over Vexa's MCP
server.

A small bot joins a Discord voice channel, receives **each speaker as a separate audio stream**
(native diarization — no clustering), transcribes every utterance on the Whisper worker Vexa
already runs, and writes the rows straight into Vexa's Postgres. On the dashboard / API / MCP, a
Discord call then looks just like any other meeting.

## The DAVE / E2EE problem

Since March 2026, Discord enforces **E2EE on all voice via the DAVE protocol**. Off-the-shelf
Python/JS voice libraries can't decrypt it, so this bridge implements the DAVE **receive** path
itself — the voice-gateway handshake, the MLS group join, and per-sender frame decryption (via
[`dave.py`](https://pypi.org/project/dave.py/), the libdave binding).

## Data-flow pipeline

```
DAVE/E2EE voice
  -> voice gateway v8 + MLS group join (per-sender key ratchets)
  -> transport decrypt (AES-256-GCM / XChaCha20, rtpsize)
  -> DAVE frame decrypt (libdave via dave.py)
  -> Opus decode -> 48 kHz PCM
  -> downsample to 16 kHz
  -> silence-gap segmenter (one utterance per speaker)
  -> POST to Vexa's transcription worker (Whisper)
  -> INSERT into meetings / meeting_sessions / transcriptions  (platform = "discord")
```

The downstream is exactly what Vexa's own Zoom/Meet bots use, so Discord calls appear in the
dashboard, the REST API, and the MCP server without special handling — **provided Vexa knows the
`discord` platform** (see [Vexa version targeting](usage/vexa-version-targeting.md)).

## Where to next

- [Deploy the bridge](usage/deploy.md)
- [Configuration & environment variables](usage/config.md)
- [DAVE receive architecture](dev/architecture.md)
- [Repository layout](dev/layout.md)
