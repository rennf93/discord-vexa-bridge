# DAVE receive architecture

The hard part — and why this repo exists: since March 2026 Discord enforces **E2EE on all voice
via the DAVE protocol**. Off-the-shelf Python/JS voice libraries can't decrypt it, so this bridge
implements the DAVE **receive** path itself and feeds the same downstream Vexa's own bots use.

## Pipeline

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

## Two encryption layers, don't conflate them

- **Transport** (client ↔ SFU): AES-256-GCM / XChaCha20 with the `secret_key` from voice-gateway
  op 4 Session Description. You must decrypt this first to get the RTP payload. Always present,
  E2EE or not. Lives in `dave_voice/transport.py`.
- **DAVE / E2EE** (frame level): inside the transport-decrypted Opus payload. Only present when
  `dave_protocol_version >= 1`. This is the new layer. Lives in `dave_voice/mls.py` (group join +
  per-sender key ratchets) + `dave.py`/libdave (frame decrypt).

## Voice gateway v8

`dave_voice/voice_ws.py` + `dave_voice/voice_client.py` own the voice WebSocket at gateway version
8 (`?v=8`). The handshake: op 0 Identify (with `max_dave_protocol_version: 1`) → op 8 Hello →
op 3 Heartbeat (carrying `seq_ack`) → op 2 Ready → IP discovery → op 1 Select Protocol (choosing
`aead_aes256_gcm_rtpsize` or `aead_xchacha20_poly1305_rtpsize`) → op 4 Session Description
(`secret_key` + negotiated `dave_protocol_version`).

`dave_voice/discord_protocol.py` is a py-cord `VoiceProtocol` shim that captures the handshake
creds (token / endpoint / `session_id`) from the main-gateway `VOICE_STATE_UPDATE` /
`VOICE_SERVER_UPDATE` dispatch; the real voice WS+UDP is owned by `DAVEVoiceClient`.

## MLS group join

`dave_voice/mls.py` joins the call's MLS group via the voice-gateway binary opcodes (the gateway
acts as the MLS external sender — only its proposals are processed). The MLS group state machine
and per-sender decryptor are provided by `dave.py` (the libdave binding). See the
[DAVE implementation notes](https://github.com/rennf93/discord-vexa-bridge/blob/master/DAVE_IMPL.md)
for the full opcode table and the frame layout (trailing `0xFAFA` magic marker, truncated GCM tag,
ULEB128 nonce, per-epoch ratcheted sender secret).

## Opus decode

`dave_voice/opus_decode.py` decodes plaintext Opus → 48 kHz PCM (decode with `fec=False`). The
PCM is exactly what the existing pipeline expects.

## Downstream (unchanged from Vexa's own bots)

Everything below "decoded per-user PCM" is the same as Vexa's Zoom/Meet bots:

- silence-gap segmenter (Discord only sends voice packets while a user is transmitting, so a gap
  with no packets ends an utterance),
- POST to `http://transcription-worker:8000/v1/audio/transcriptions`,
- `meetings (platform='discord')` / `meeting_sessions` / `transcriptions` inserts with
  `speaker` = Discord display name,
- the `/join` / `/leave` slash commands and the DB schema.

The SSRC→user map (from op 5 Speaking events) replaces py-cord's per-user sink routing; the
display-name lookup (`Meeting.name_for`) stays as-is.

## Further reading

- [DAVE implementation notes](https://github.com/rennf93/discord-vexa-bridge/blob/master/DAVE_IMPL.md) —
  the full receive-path design spec (opcode table, frame layout, milestone sequence).
- DAVE protocol whitepaper — <https://daveprotocol.com>
- Protocol repo / test vectors — <https://github.com/discord/dave-protocol>
- Reference implementation (C++/JS) — <https://github.com/discord/libdave>
- MLS — [RFC 9420](https://www.rfc-editor.org/rfc/rfc9420.html)