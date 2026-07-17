# Repository layout

```
bot.py            control plane: slash commands (/join, /leave), the silence-gap segmenter,
                  the transcription call, and the Postgres writes
dave_voice/       the voice-receive internals (the DAVE receive path):
  discord_protocol.py   py-cord VoiceProtocol shim — captures handshake creds
                        (token / endpoint / session_id); the real voice WS+UDP is owned
                        by DAVEVoiceClient
  voice_ws.py          voice gateway v8
  voice_client.py      voice WS+UDP + the receive pipeline
  transport.py         rtpsize transport decrypt (AES-256-GCM / XChaCha20)
  mls.py               MLS group join + per-sender key ratchets
  rtp.py               RTP parsing
  udp_receiver.py      UDP receive loop
  ip_discovery.py      IP discovery
  opcodes.py           voice gateway + DAVE binary opcodes
  opus_decode.py       per-SSRC Opus -> 48 kHz PCM (decode with fec=False)
tests/            unit tests (pytest, asyncio_mode=auto). No network or live Discord required.
```

`bot.py` is the control plane — slash commands, the silence-gap segmenter, the transcription
call, and the Postgres writes. `dave_voice/` is the voice-receive internals that replaced the
broken py-cord receive layer. See [Architecture](architecture.md) for how the pieces fit.
