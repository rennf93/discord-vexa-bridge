# Limitations

## Python is pinned to 3.11

The bot uses the stdlib `audioop` module, **removed in Python 3.13**. The runtime and base image
are pinned to 3.11 — do not bump past 3.11.

## Transcription latency = your Whisper worker

On CPU with `large-v3-turbo` a short utterance can take tens of seconds; drop the worker's
`MODEL_SIZE` to `small`/`medium`, or give it a GPU, for snappier results. Segments are durable
(spilled to `/data/pending`), so a slow worker delays rows but never drops them.

## DAVE is a moving, Discord-controlled protocol

The decrypt path tracks the published spec via `dave.py`/libdave; protocol bumps may need
updates to the decrypt path / `dave.py`. DAVE is versioned and Discord-controlled — the gateway
selects the lowest shared version, and you must retain backwards-compat for non-discontinued
versions. This is ongoing maintenance, not a one-shot. See the protocol references at
<https://daveprotocol.com>.

## Frame decrypt failures at epoch edges

A handful of frames fail to decrypt at stream start / epoch edges — harmless; the successful
frames produce accurate transcripts.

## Vexa dashboard meeting-detail view

The Vexa dashboard meeting-detail view is built around its own bot pipeline; the transcript reads
fine via API/MCP, but that page may need the `discord` platform enum patch (README Step 2 /
[Vexa version targeting](vexa-version-targeting.md)) to render cleanly.

## Single-bot / single-Vexa by design

This bridge is single-bot / single-Vexa by design. Offering it as a shared, multi-server hosted
product would be a different (multi-tenant) architecture and is not what this repo does.