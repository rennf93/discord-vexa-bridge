# DAVE Voice Receive Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace py-cord's broken voice-receive layer with a from-scratch gateway-v8 voice client that decrypts DAVE (E2EE) voice per user and feeds the existing `store()` → Whisper → `transcriptions` pipeline unchanged.

**Architecture:** Keep py-cord as the *control plane* (slash commands, main gateway, member-name lookups, the `change_voice_state` op-4 trigger). Replace only the voice connection. A new `dave_voice/` package opens the voice WebSocket at `?v=8`, runs the heartbeat/`seq_ack` loop, performs IP discovery + Select Protocol, decrypts the transport layer (AES-256-GCM / XChaCha20, rtpsize), runs the MLS group handshake via the `dave.py` binding (libdave), exports per-sender key ratchets, decrypts DAVE frames with `dave.py`'s `Decryptor`, Opus-decodes to 48 kHz stereo PCM, and emits `(user_id, pcm, t_start, t_end)` tuples into the existing pipeline.

**Tech Stack:** Python 3.11, py-cord (control plane + `discord.opus` decoder), `dave.py` (libdave MLS + frame crypto, PyPI wheels), `websockets` (voice WS), `cryptography` (AES-256-GCM transport), `PyNaCl` (XChaCha20-Poly1305 fallback transport), `asyncio` DatagramProtocol (RTP receive), `pytest`/`pytest-asyncio` (tests). Postgres via `asyncpg` (unchanged), Whisper via `aiohttp` (unchanged).

## Global Constraints

- **Base image / Python:** stay on `python:3.11-slim`. `audioop` (used by `to_mono_wav`) is stdlib on 3.11 and removed in 3.13 — do **not** bump Python.
- **Receive only.** Never send/encrypt media. Implement only the read half of DAVE. Out of scope: sending media, video, screen share, sender ratchet-for-sending.
- **Do not reimplement MLS or the frame cipher.** All RFC-9420 / AES-128-GCM frame crypto comes from `dave.py` (`Session`, `Decryptor`, `SignatureKeyPair`). Pure-Python code in this plan is *protocol glue only*: WebSocket framing, opcode routing, transport-layer decrypt, RTP parsing, Opus decode, and wiring.
- **External sender trust:** in the MLS group the voice gateway is the external sender. Only process Add/Remove proposals from it (this is enforced inside `dave.py` via the `recognized_user_ids` set and `set_external_sender`); never trust proposals attributed to other members.
- **Transport modes:** advertise and prefer `aead_aes256_gcm_rtpsize`; support `aead_xchacha20_poly1305_rtpsize` as fallback. These are the only two non-deprecated modes.
- **Identify must advertise DAVE:** send `max_dave_protocol_version` = `dave.get_max_supported_protocol_version()` in op-0 Identify. Omitting it (or sending 0) means no E2EE negotiation and you will be rejected/downgraded on E2EE calls.
- **PCM contract (unchanged):** the existing pipeline expects 48 000 Hz, 16-bit, **stereo** PCM bytes. `SR, CH, SW = 48_000, 2, 2`. Emit exactly that so `to_mono_wav`, `store`, and the DB inserts stay untouched.
- **Two encryption layers, never conflate:** (1) transport (client↔SFU) using the op-4 `secret_key`, always present; (2) DAVE/E2EE frame layer inside the transport-decrypted payload, present only when `dave_protocol_version ≥ 1`. Decrypt transport first, then hand the payload to `dave.py`'s `Decryptor`.
- **Tests must not hit live Discord.** Pure units are TDD'd with constructed vectors. Live integration is verified by the operator at the marked **LIVE CHECKPOINT** steps using a real bot token on a DAVE-enforced channel.

---

## Reference constants (authoritative, used across tasks)

Voice gateway opcodes:

| Op | Name | Dir | Format |
|----|------|-----|--------|
| 0  | Identify | C→S | JSON |
| 1  | Select Protocol | C→S | JSON |
| 2  | Ready | S→C | JSON |
| 3  | Heartbeat | C→S | JSON (`{t, seq_ack}`) |
| 4  | Session Description | S→C | JSON (`secret_key`, `mode`, `dave_protocol_version`) |
| 5  | Speaking | both | JSON (`speaking`, `ssrc`, `user_id`) |
| 6  | Heartbeat ACK | S→C | JSON |
| 7  | Resume | C→S | JSON |
| 8  | Hello | S→C | JSON (`heartbeat_interval`) |
| 9  | Resumed | S→C | JSON |
| 11 | Clients Connect | S→C | JSON (`user_ids`) |
| 13 | Client Disconnect | S→C | JSON (`user_id`) |
| 21 | DAVE Prepare Transition | S→C | JSON (`protocol_version`, `transition_id`) |
| 22 | DAVE Execute Transition | S→C | JSON (`transition_id`) |
| 23 | DAVE Transition Ready | C→S | JSON (`transition_id`) |
| 24 | DAVE Prepare Epoch | S→C | JSON (`protocol_version`, `epoch`) |
| 25 | MLS External Sender Package | S→C | **binary** |
| 26 | MLS Key Package | C→S | **binary** |
| 27 | MLS Proposals | S→C | **binary** |
| 28 | MLS Commit+Welcome | C→S | **binary** |
| 29 | MLS Announce Commit Transition | S→C | **binary** |
| 30 | MLS Welcome | S→C | **binary** |
| 31 | MLS Invalid Commit/Welcome | C→S | JSON (`transition_id`) |

Binary server→client framing: `[uint16 BE sequence][uint8 opcode][payload]`. Track the sequence for `seq_ack` (heartbeats) and buffered resume. JSON messages are normal `{"op":..,"d":..,"seq"?:..}` text frames.

Transport mode strings: `aead_aes256_gcm_rtpsize` (prefer), `aead_xchacha20_poly1305_rtpsize` (fallback).

RTP header (12 bytes, big-endian): `[u8 version+flags][u8 payload_type][u16 sequence][u32 timestamp][u32 ssrc]`, payload at offset 12.

rtpsize transport decryption: the 12-byte RTP header is **AAD**; the **last 4 bytes** of the UDP packet are the truncated nonce (a 32-bit incrementing counter), expanded to the cipher nonce by left-padding with zero bytes; the ciphertext+auth-tag sit between header end and the trailing 4-byte nonce.

IP discovery packet (big-endian, 74 bytes total): `[u16 type (0x1 req / 0x2 resp)][u16 length=70][u32 ssrc][64-byte null-terminated address][u16 port]`.

`dave.py` API actually used (verified from `src/dave/_dave_impl.pyi`):
- `dave.get_max_supported_protocol_version() -> int`
- `dave.MediaType.audio` (== 0), `dave.Codec.opus`
- `dave.SignatureKeyPair.generate(version)`
- `dave.Session(mls_failure_callback=None)` with: `init(version, group_id, self_user_id, transient_key=None)`, `reset()`, `set_protocol_version(v)`, `get_protocol_version()`, `set_external_sender(bytes)`, `process_proposals(bytes, recognized_user_ids:set[str]) -> bytes|None`, `process_commit(bytes) -> RejectType|dict`, `process_welcome(bytes, recognized_user_ids) -> dict|None`, `get_marshalled_key_package() -> bytes`, `get_key_ratchet(user_id:str) -> IKeyRatchet|None`, `has_established_group() -> bool`
- `dave.Decryptor()` with: `transition_to_key_ratchet(ratchet, transition_expiry=...)`, `transition_to_passthrough_mode(bool, transition_expiry=...)`, `decrypt(media_type, frame:bytes) -> bytes|None`

---

## File Structure

```
dave_voice/
  __init__.py          # exports DAVEVoiceClient
  opcodes.py           # VoiceOp IntEnum, BINARY_SERVER_OPS set, frame encode/decode helpers
  ip_discovery.py      # build_request() / parse_response()
  rtp.py               # parse_rtp_header(), RtpPacket dataclass
  transport.py         # TransportCrypto: decrypt(mode, secret_key, packet) -> payload bytes
  mls.py               # MLSManager: wraps dave.Session + per-user Decryptor registry
  voice_ws.py          # VoiceGateway: WebSocket v8 lifecycle, opcode routing, heartbeat/seq_ack
  udp_receiver.py      # asyncio DatagramProtocol; demux packets by SSRC to a callback
  opus_decode.py       # OpusDecoders: per-SSRC discord.opus.Decoder -> 48k stereo PCM
  voice_client.py      # DAVEVoiceClient: orchestrates WS+UDP+transport+MLS+opus -> emits tuples
bot.py                 # MODIFIED: control-plane wiring; /join spins up DAVEVoiceClient
tests/
  test_opcodes.py
  test_ip_discovery.py
  test_rtp.py
  test_transport.py
  test_mls.py
  test_dave_binding.py
  test_voice_ws_routing.py
Dockerfile             # MODIFIED: deps
requirements.txt       # NEW: pinned deps
```

Each `dave_voice/*` module has one responsibility. `voice_client.py` is the only place that wires them together; it exposes the same `(user_id, pcm, t_start, t_end)` contract the existing `LiveSink`/`flusher` consumed, so the downstream pipeline is untouched.

---

## Task 1: Project scaffolding & dependencies

**Files:**
- Create: `requirements.txt`
- Create: `dave_voice/__init__.py`
- Create: `tests/__init__.py`
- Modify: `Dockerfile`
- Create: `pytest.ini`

**Interfaces:**
- Produces: an installable dev environment where `import dave`, `import nacl`, `from cryptography.hazmat.primitives.ciphers.aead import AESGCM`, `import websockets`, and `pytest` all work.

- [ ] **Step 1: Write `requirements.txt`**

```text
py-cord[voice]
asyncpg
aiohttp
dave.py
websockets
cryptography
PyNaCl
```

- [ ] **Step 2: Write `pytest.ini`**

```ini
[pytest]
asyncio_mode = auto
testpaths = tests
```

- [ ] **Step 3: Create empty package markers**

`dave_voice/__init__.py`:

```python
"""From-scratch DAVE-capable Discord voice receive client."""
```

`tests/__init__.py`: empty file.

- [ ] **Step 4: Install and verify the binding imports**

Run:
```bash
pip install dave.py pytest pytest-asyncio cryptography PyNaCl websockets
python -c "import dave; print('max dave version:', dave.get_max_supported_protocol_version())"
```
Expected: prints `max dave version: 1` (or higher). If `dave` fails to import on this platform, stop — record the platform; prebuilt wheels cover `manylinux/musllinux x86_64/aarch64`, macOS `x86_64/arm64`, win `amd64/arm64` only.

- [ ] **Step 5: Update `Dockerfile`**

```dockerfile
# py3.11: audioop is still present here (removed in 3.13).
FROM python:3.11-slim

# libopus is needed to DECODE incoming Discord voice.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
COPY dave_voice ./dave_voice
CMD ["python", "bot.py"]
```

- [ ] **Step 6: Commit**

```bash
git add requirements.txt pytest.ini dave_voice/__init__.py tests/__init__.py Dockerfile
git commit -m "chore: scaffold dave_voice package and add DAVE deps"
```

---

## Task 2: Opcodes & binary frame helpers

**Files:**
- Create: `dave_voice/opcodes.py`
- Test: `tests/test_opcodes.py`

**Interfaces:**
- Produces:
  - `class VoiceOp(IntEnum)` with every opcode from the reference table.
  - `BINARY_SERVER_OPS: frozenset[int]` = `{25, 27, 29, 30}` (server→client binary opcodes carrying a uint16 seq prefix).
  - `decode_binary(data: bytes) -> tuple[int, int, bytes]` returning `(seq, opcode, payload)`.
  - `encode_binary(opcode: int, payload: bytes) -> bytes` returning client→server binary `[u8 opcode][payload]` (no seq prefix on the client→server direction).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_opcodes.py
import struct
from dave_voice.opcodes import VoiceOp, BINARY_SERVER_OPS, decode_binary, encode_binary


def test_opcode_values():
    assert VoiceOp.IDENTIFY == 0
    assert VoiceOp.SESSION_DESCRIPTION == 4
    assert VoiceOp.DAVE_MLS_EXTERNAL_SENDER == 25
    assert VoiceOp.DAVE_MLS_KEY_PACKAGE == 26
    assert VoiceOp.DAVE_MLS_WELCOME == 30
    assert VoiceOp.DAVE_MLS_INVALID_COMMIT_WELCOME == 31


def test_binary_server_ops_set():
    assert BINARY_SERVER_OPS == frozenset({25, 27, 29, 30})


def test_decode_binary_strips_seq_and_opcode():
    payload = b"\xde\xad\xbe\xef"
    data = struct.pack(">H", 7) + bytes([27]) + payload
    seq, opcode, body = decode_binary(data)
    assert seq == 7
    assert opcode == 27
    assert body == payload


def test_encode_binary_prepends_opcode_only():
    out = encode_binary(26, b"\x01\x02")
    assert out == bytes([26]) + b"\x01\x02"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_opcodes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dave_voice.opcodes'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/opcodes.py
"""Voice gateway opcodes and binary frame (de)framing for DAVE."""
import struct
from enum import IntEnum


class VoiceOp(IntEnum):
    IDENTIFY = 0
    SELECT_PROTOCOL = 1
    READY = 2
    HEARTBEAT = 3
    SESSION_DESCRIPTION = 4
    SPEAKING = 5
    HEARTBEAT_ACK = 6
    RESUME = 7
    HELLO = 8
    RESUMED = 9
    CLIENTS_CONNECT = 11
    CLIENT_DISCONNECT = 13
    DAVE_PREPARE_TRANSITION = 21
    DAVE_EXECUTE_TRANSITION = 22
    DAVE_TRANSITION_READY = 23
    DAVE_PREPARE_EPOCH = 24
    DAVE_MLS_EXTERNAL_SENDER = 25
    DAVE_MLS_KEY_PACKAGE = 26
    DAVE_MLS_PROPOSALS = 27
    DAVE_MLS_COMMIT_WELCOME = 28
    DAVE_MLS_ANNOUNCE_COMMIT = 29
    DAVE_MLS_WELCOME = 30
    DAVE_MLS_INVALID_COMMIT_WELCOME = 31


# Server->client binary opcodes carry a 2-byte big-endian sequence prefix.
BINARY_SERVER_OPS = frozenset({25, 27, 29, 30})


def decode_binary(data: bytes) -> tuple[int, int, bytes]:
    """[u16 BE seq][u8 opcode][payload] -> (seq, opcode, payload)."""
    seq = struct.unpack_from(">H", data, 0)[0]
    opcode = data[2]
    return seq, opcode, data[3:]


def encode_binary(opcode: int, payload: bytes) -> bytes:
    """Client->server binary message: [u8 opcode][payload] (no seq prefix)."""
    return bytes([opcode]) + payload
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_opcodes.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add dave_voice/opcodes.py tests/test_opcodes.py
git commit -m "feat: voice opcodes and binary frame helpers"
```

> **LIVE VERIFICATION NOTE:** whether JSON messages also carry an in-band `seq` field (vs only binary frames) is confirmed against live traffic in Task 9. The `VoiceGateway` tracks the last seq from binary frames regardless; this is sufficient for `seq_ack`.

---

## Task 3: IP discovery packet

**Files:**
- Create: `dave_voice/ip_discovery.py`
- Test: `tests/test_ip_discovery.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `build_request(ssrc: int) -> bytes` — 74-byte discovery request.
  - `parse_response(data: bytes) -> tuple[str, int]` — returns `(external_ip, external_port)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ip_discovery.py
import struct
from dave_voice.ip_discovery import build_request, parse_response


def test_build_request_layout():
    pkt = build_request(0x11223344)
    assert len(pkt) == 74
    typ, length, ssrc = struct.unpack_from(">HHI", pkt, 0)
    assert typ == 0x1
    assert length == 70
    assert ssrc == 0x11223344


def test_parse_response_roundtrip():
    # Build a synthetic response: type=2, len=70, ssrc, 64-byte addr, port
    addr = b"203.0.113.7" + b"\x00" * (64 - len("203.0.113.7"))
    resp = struct.pack(">HHI", 0x2, 70, 0x11223344) + addr + struct.pack(">H", 50001)
    ip, port = parse_response(resp)
    assert ip == "203.0.113.7"
    assert port == 50001
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_ip_discovery.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/ip_discovery.py
"""Discord UDP IP discovery (find our public ip/port for Select Protocol)."""
import struct


def build_request(ssrc: int) -> bytes:
    # [u16 type=1][u16 length=70][u32 ssrc][64 addr zeros][u16 port=0]
    return struct.pack(">HHI", 0x1, 70, ssrc) + b"\x00" * 64 + struct.pack(">H", 0)


def parse_response(data: bytes) -> tuple[str, int]:
    # type(2) length(2) ssrc(4) addr(64) port(2)
    addr = data[8:72].split(b"\x00", 1)[0].decode("ascii")
    port = struct.unpack_from(">H", data, 72)[0]
    return addr, port
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_ip_discovery.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add dave_voice/ip_discovery.py tests/test_ip_discovery.py
git commit -m "feat: UDP IP discovery request/response"
```

---

## Task 4: RTP header parsing

**Files:**
- Create: `dave_voice/rtp.py`
- Test: `tests/test_rtp.py`

**Interfaces:**
- Produces:
  - `@dataclass RtpPacket` with fields `version_flags:int, payload_type:int, sequence:int, timestamp:int, ssrc:int, header:bytes, body:bytes` (`header` = first 12 bytes, `body` = bytes after the header).
  - `parse_rtp_header(data: bytes) -> RtpPacket`.
  - `HEADER_LEN = 12`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rtp.py
import struct
from dave_voice.rtp import parse_rtp_header, HEADER_LEN


def test_parse_header_fields():
    header = struct.pack(">BBHII", 0x80, 0x78, 1234, 0xAABBCCDD, 0x01020304)
    body = b"opuspayload"
    pkt = parse_rtp_header(header + body)
    assert pkt.version_flags == 0x80
    assert pkt.payload_type == 0x78
    assert pkt.sequence == 1234
    assert pkt.timestamp == 0xAABBCCDD
    assert pkt.ssrc == 0x01020304
    assert pkt.header == header
    assert pkt.body == body
    assert HEADER_LEN == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_rtp.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/rtp.py
"""Minimal RTP header parsing for received Discord voice packets."""
import struct
from dataclasses import dataclass

HEADER_LEN = 12


@dataclass
class RtpPacket:
    version_flags: int
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    header: bytes
    body: bytes


def parse_rtp_header(data: bytes) -> RtpPacket:
    vf, pt, seq, ts, ssrc = struct.unpack_from(">BBHII", data, 0)
    return RtpPacket(
        version_flags=vf,
        payload_type=pt,
        sequence=seq,
        timestamp=ts,
        ssrc=ssrc,
        header=data[:HEADER_LEN],
        body=data[HEADER_LEN:],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_rtp.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dave_voice/rtp.py tests/test_rtp.py
git commit -m "feat: RTP header parsing"
```

> **LIVE VERIFICATION NOTE:** RTP header extensions (when `version_flags & 0x10`) add a variable extension block after the 12-byte fixed header. Task 6 handles stripping it before transport decrypt; the fixed-header parse here is always 12 bytes.

---

## Task 5: Transport-layer decryption (AES-256-GCM rtpsize + XChaCha20 fallback)

**Files:**
- Create: `dave_voice/transport.py`
- Test: `tests/test_transport.py`

**Interfaces:**
- Consumes: `dave_voice.rtp.HEADER_LEN`.
- Produces:
  - `SUPPORTED_MODES = ("aead_aes256_gcm_rtpsize", "aead_xchacha20_poly1305_rtpsize")` (preference order).
  - `class TransportCrypto` constructed with `__init__(self, mode: str, secret_key: bytes)`.
  - `TransportCrypto.decrypt(self, packet: bytes) -> bytes` — takes a full received UDP RTP packet (fixed 12-byte header assumed; extension stripping done by caller in Task 6) and returns the decrypted media payload (the bytes the DAVE `Decryptor` consumes).

rtpsize rule: AAD = the RTP header (everything before the encrypted payload); the **last 4 bytes** of the packet are the truncated nonce; ciphertext+tag are between header-end and the trailing 4 nonce bytes. AES-256-GCM nonce = 12 bytes = `b"\x00"*8 + nonce4`. XChaCha20-Poly1305-ietf nonce = 24 bytes = `b"\x00"*20 + nonce4`.

- [ ] **Step 1: Write the failing test (round-trip both modes by self-encrypting)**

```python
# tests/test_transport.py
import os
import struct
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from nacl import bindings
from dave_voice.transport import TransportCrypto, SUPPORTED_MODES


def _rtp_header():
    return struct.pack(">BBHII", 0x80, 0x78, 1, 2, 3)


def test_aes256_gcm_rtpsize_roundtrip():
    key = os.urandom(32)
    header = _rtp_header()
    plaintext = b"decoded-opus-frame-bytes"
    nonce4 = struct.pack(">I", 5)
    ct = AESGCM(key).encrypt(b"\x00" * 8 + nonce4, plaintext, header)
    packet = header + ct + nonce4
    out = TransportCrypto("aead_aes256_gcm_rtpsize", key).decrypt(packet)
    assert out == plaintext


def test_xchacha20_poly1305_rtpsize_roundtrip():
    key = os.urandom(32)
    header = _rtp_header()
    plaintext = b"decoded-opus-frame-bytes"
    nonce4 = struct.pack(">I", 9)
    full_nonce = b"\x00" * 20 + nonce4
    ct = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(
        plaintext, header, full_nonce, key
    )
    packet = header + ct + nonce4
    out = TransportCrypto("aead_xchacha20_poly1305_rtpsize", key).decrypt(packet)
    assert out == plaintext


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        TransportCrypto("xsalsa20_poly1305", os.urandom(32))


def test_supported_modes_preference_order():
    assert SUPPORTED_MODES[0] == "aead_aes256_gcm_rtpsize"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_transport.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/transport.py
"""Transport-layer (client<->SFU) decryption for rtpsize AEAD modes."""
import struct
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from nacl import bindings
from dave_voice.rtp import HEADER_LEN

SUPPORTED_MODES = (
    "aead_aes256_gcm_rtpsize",
    "aead_xchacha20_poly1305_rtpsize",
)
NONCE_TRUNC_LEN = 4


class TransportCrypto:
    def __init__(self, mode: str, secret_key: bytes):
        if mode not in SUPPORTED_MODES:
            raise ValueError(f"unsupported transport mode: {mode}")
        self.mode = mode
        self.key = bytes(secret_key)

    def decrypt(self, packet: bytes, header_len: int = HEADER_LEN) -> bytes:
        nonce4 = packet[-NONCE_TRUNC_LEN:]
        header = packet[:header_len]
        ciphertext = packet[header_len:-NONCE_TRUNC_LEN]
        if self.mode == "aead_aes256_gcm_rtpsize":
            full_nonce = b"\x00" * 8 + nonce4
            return AESGCM(self.key).decrypt(full_nonce, ciphertext, header)
        full_nonce = b"\x00" * 20 + nonce4
        return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            ciphertext, header, full_nonce, self.key
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_transport.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add dave_voice/transport.py tests/test_transport.py
git commit -m "feat: transport AEAD decryption (aes256-gcm + xchacha20, rtpsize)"
```

---

## Task 6: `dave.py` binding smoke test

**Files:**
- Test: `tests/test_dave_binding.py`

**Interfaces:**
- Consumes: the installed `dave` module.
- Produces: confidence that `Session`, `Decryptor`, `SignatureKeyPair`, and the enums exist with the signatures this plan relies on. This task replaces the old spec's "build libdave + pass C++ test vectors" milestone.

- [ ] **Step 1: Write the test**

```python
# tests/test_dave_binding.py
import dave


def test_max_version_at_least_1():
    assert dave.get_max_supported_protocol_version() >= 1


def test_media_type_and_codec_enums():
    assert int(dave.MediaType.audio) == 0
    assert hasattr(dave.Codec, "opus")


def test_signature_keypair_generates():
    kp = dave.SignatureKeyPair.generate(dave.get_max_supported_protocol_version())
    assert kp is not None


def test_session_constructs_and_reports_no_group():
    s = dave.Session()
    assert s.has_established_group() is False


def test_decryptor_constructs_and_passthrough_decrypt_is_callable():
    d = dave.Decryptor()
    # In passthrough mode an un-encrypted frame should pass through unchanged.
    d.transition_to_passthrough_mode(True, 0.0)
    out = d.decrypt(dave.MediaType.audio, b"\x01\x02\x03")
    assert out == b"\x01\x02\x03"
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_dave_binding.py -v`
Expected: PASS. If `test_decryptor_constructs_and_passthrough_decrypt_is_callable` fails on the passthrough return value, adjust the assertion to match the binding's actual passthrough behavior and record the observed semantics in a comment — the binding is authoritative, this test documents it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_dave_binding.py
git commit -m "test: dave.py binding smoke test (replaces libdave build milestone)"
```

---

## Task 7: MLS manager (group handshake + per-user decryptor registry)

**Files:**
- Create: `dave_voice/mls.py`
- Test: `tests/test_mls.py`

**Interfaces:**
- Consumes: `dave` module; `dave_voice.opcodes.VoiceOp`.
- Produces: `class MLSManager` that owns one `dave.Session` and a `dict[int, dave.Decryptor]` keyed by SSRC. It does **not** touch the network — it returns `(opcode, payload_bytes)` reply tuples (or `None`) for the caller (`VoiceGateway`) to send. Public API:
  - `__init__(self, self_user_id: int)`
  - `set_version(self, version: int) -> None`
  - `recognized_user_ids: set[str]` (caller updates from Speaking / Clients Connect)
  - `on_external_sender(self, package: bytes) -> None`
  - `on_prepare_epoch(self, version: int, epoch: int) -> tuple[int, bytes] | None` — when `epoch == 1`, inits the group and returns `(VoiceOp.DAVE_MLS_KEY_PACKAGE, key_package_bytes)`.
  - `on_proposals(self, blob: bytes) -> tuple[int, bytes] | None` — returns `(DAVE_MLS_COMMIT_WELCOME, commit_bytes)` if a commit was produced.
  - `on_announce_commit(self, transition_id: int, commit: bytes) -> tuple[int, bytes]` — returns `(DAVE_TRANSITION_READY, transition_id_json_bytes)`; on reject returns `(DAVE_MLS_INVALID_COMMIT_WELCOME, ...)`.
  - `on_welcome(self, transition_id: int, welcome: bytes) -> tuple[int, bytes]` — same ready/invalid logic.
  - `refresh_ratchets(self, ssrc_to_user: dict[int, int]) -> None` — for each known SSRC, fetch `session.get_key_ratchet(str(user_id))` and `decryptor.transition_to_key_ratchet(...)`.
  - `decryptor_for(self, ssrc: int) -> dave.Decryptor` — lazily creates a `Decryptor` per SSRC.
  - `set_group_id(self, group_id: int)` — the channel id used as MLS group id.

  Reply payloads for JSON opcodes (23, 31) are returned as already-encoded JSON `bytes` so the gateway can send them as text frames; the gateway distinguishes by opcode. Binary opcodes (26, 28) return raw MLS bytes.

  NOTE: the precise `set_external_sender` input framing and `init` argument values are confirmed at the LIVE CHECKPOINT in Task 11. This task's unit tests use a fake Session to lock the *routing logic*, which is the part that's testable without Discord.

- [ ] **Step 1: Write the failing test (routing logic against a fake Session)**

```python
# tests/test_mls.py
import json
import pytest
import dave_voice.mls as mls_mod
from dave_voice.mls import MLSManager
from dave_voice.opcodes import VoiceOp


class FakeRatchet:
    pass


class FakeSession:
    def __init__(self, *a, **k):
        self.inited = None
        self.external = None
        self.version = 0
        self._ratchets = {}
        self.established = False

    def init(self, version, group_id, self_user_id, transient_key=None):
        self.inited = (version, group_id, self_user_id)
        self.established = True

    def set_protocol_version(self, v):
        self.version = v

    def set_external_sender(self, pkg):
        self.external = pkg

    def get_marshalled_key_package(self):
        return b"KP"

    def process_proposals(self, proposals, recognized):
        return b"COMMIT" if proposals == b"adds" else None

    def process_commit(self, commit):
        return {1: [0]}  # roster-ish success (not a RejectType)

    def process_welcome(self, welcome, recognized):
        return {1: [0]}

    def get_key_ratchet(self, user_id):
        return self._ratchets.get(user_id)

    def has_established_group(self):
        return self.established


class FakeDecryptor:
    def __init__(self):
        self.ratchet = None

    def transition_to_key_ratchet(self, ratchet, transition_expiry=0.0):
        self.ratchet = ratchet


@pytest.fixture(autouse=True)
def patch_dave(monkeypatch):
    monkeypatch.setattr(mls_mod.dave, "Session", FakeSession)
    monkeypatch.setattr(mls_mod.dave, "Decryptor", FakeDecryptor)
    # RejectType used in isinstance checks; give it a sentinel class
    class RejectType:  # noqa
        failed = object()
        ignored = object()
    monkeypatch.setattr(mls_mod.dave, "RejectType", RejectType)


def test_prepare_epoch_1_inits_and_returns_key_package():
    m = MLSManager(self_user_id=42)
    m.set_group_id(999)
    m.set_version(1)
    reply = m.on_prepare_epoch(version=1, epoch=1)
    assert reply == (VoiceOp.DAVE_MLS_KEY_PACKAGE, b"KP")
    assert m.session.inited == (1, 999, "42")


def test_prepare_epoch_gt1_no_keypackage():
    m = MLSManager(self_user_id=42)
    m.set_group_id(999)
    m.set_version(1)
    m.on_prepare_epoch(version=1, epoch=1)
    assert m.on_prepare_epoch(version=1, epoch=2) is None


def test_proposals_producing_commit():
    m = MLSManager(self_user_id=42)
    m.set_group_id(999); m.set_version(1); m.on_prepare_epoch(1, 1)
    reply = m.on_proposals(b"adds")
    assert reply == (VoiceOp.DAVE_MLS_COMMIT_WELCOME, b"COMMIT")


def test_proposals_no_commit_returns_none():
    m = MLSManager(self_user_id=42)
    m.set_group_id(999); m.set_version(1); m.on_prepare_epoch(1, 1)
    assert m.on_proposals(b"revoke") is None


def test_announce_commit_returns_transition_ready_json():
    m = MLSManager(self_user_id=42)
    m.set_group_id(999); m.set_version(1); m.on_prepare_epoch(1, 1)
    op, payload = m.on_announce_commit(transition_id=5, commit=b"C")
    assert op == VoiceOp.DAVE_TRANSITION_READY
    assert json.loads(payload) == {"transition_id": 5}


def test_refresh_ratchets_assigns_per_ssrc():
    m = MLSManager(self_user_id=42)
    m.set_group_id(999); m.set_version(1); m.on_prepare_epoch(1, 1)
    r = FakeRatchet()
    m.session._ratchets["7"] = r
    m.refresh_ratchets({1234: 7})
    assert m.decryptor_for(1234).ratchet is r
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_mls.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dave_voice.mls'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/mls.py
"""MLS group orchestration over dave.py's Session + per-SSRC Decryptor registry.

Pure routing/state logic: methods return (opcode, payload_bytes) reply tuples or
None; the caller (VoiceGateway) does the actual network send. All RFC-9420 / frame
crypto lives inside dave.py.
"""
import json

import dave

from dave_voice.opcodes import VoiceOp


class MLSManager:
    def __init__(self, self_user_id: int):
        self.self_user_id = self_user_id
        self.session = dave.Session()
        self.decryptors: dict[int, dave.Decryptor] = {}
        self.recognized_user_ids: set[str] = set()
        self.version = 1
        self.group_id = 0
        self._initialized = False

    def set_group_id(self, group_id: int) -> None:
        self.group_id = group_id

    def set_version(self, version: int) -> None:
        self.version = version
        if self._initialized:
            self.session.set_protocol_version(version)

    def on_external_sender(self, package: bytes) -> None:
        self.session.set_external_sender(package)

    def on_prepare_epoch(self, version: int, epoch: int):
        self.version = version
        if epoch == 1:
            self.session.init(version, self.group_id, str(self.self_user_id))
            self._initialized = True
            return (VoiceOp.DAVE_MLS_KEY_PACKAGE, self.session.get_marshalled_key_package())
        # epoch > 1: protocol-version change of the existing group
        if self._initialized:
            self.session.set_protocol_version(version)
        return None

    def on_proposals(self, blob: bytes):
        commit = self.session.process_proposals(blob, self.recognized_user_ids)
        if commit is None:
            return None
        return (VoiceOp.DAVE_MLS_COMMIT_WELCOME, commit)

    def _ready_or_invalid(self, transition_id: int, result):
        if isinstance(result, dave.RejectType):
            return (
                VoiceOp.DAVE_MLS_INVALID_COMMIT_WELCOME,
                json.dumps({"transition_id": transition_id}).encode(),
            )
        return (
            VoiceOp.DAVE_TRANSITION_READY,
            json.dumps({"transition_id": transition_id}).encode(),
        )

    def on_announce_commit(self, transition_id: int, commit: bytes):
        result = self.session.process_commit(commit)
        return self._ready_or_invalid(transition_id, result)

    def on_welcome(self, transition_id: int, welcome: bytes):
        result = self.session.process_welcome(welcome, self.recognized_user_ids)
        if result is None:
            return (
                VoiceOp.DAVE_MLS_INVALID_COMMIT_WELCOME,
                json.dumps({"transition_id": transition_id}).encode(),
            )
        return self._ready_or_invalid(transition_id, result)

    def decryptor_for(self, ssrc: int) -> "dave.Decryptor":
        d = self.decryptors.get(ssrc)
        if d is None:
            d = dave.Decryptor()
            self.decryptors[ssrc] = d
        return d

    def refresh_ratchets(self, ssrc_to_user: dict[int, int]) -> None:
        for ssrc, user_id in ssrc_to_user.items():
            ratchet = self.session.get_key_ratchet(str(user_id))
            if ratchet is not None:
                self.decryptor_for(ssrc).transition_to_key_ratchet(ratchet)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_mls.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add dave_voice/mls.py tests/test_mls.py
git commit -m "feat: MLS manager routing over dave.py Session + per-SSRC decryptors"
```

---

## Task 8: Opus decoders (per-SSRC PCM)

**Files:**
- Create: `dave_voice/opus_decode.py`
- Test: `tests/test_opus_decode.py`

**Interfaces:**
- Consumes: `discord.opus` (py-cord, already loaded by `bot.py`'s `on_ready`).
- Produces:
  - `class OpusDecoders` with `decode(self, ssrc: int, opus_bytes: bytes) -> bytes` returning 48 kHz, 16-bit, **stereo** PCM. Maintains one `discord.opus.Decoder` per SSRC (Opus decoder state is per-stream).
  - `reset(self, ssrc: int) -> None` to drop a stream's decoder on disconnect.

- [ ] **Step 1: Write the failing test (decoder factory injected for hermetic test)**

```python
# tests/test_opus_decode.py
from dave_voice.opus_decode import OpusDecoders


class FakeDecoder:
    def __init__(self):
        self.calls = []

    def decode(self, data):
        self.calls.append(data)
        # pretend each opus frame -> 4 bytes of PCM
        return b"\x00\x00\x01\x01"


def test_one_decoder_per_ssrc_and_decode():
    made = []

    def factory():
        d = FakeDecoder()
        made.append(d)
        return d

    od = OpusDecoders(decoder_factory=factory)
    out1 = od.decode(111, b"opusA")
    out2 = od.decode(111, b"opusB")
    out3 = od.decode(222, b"opusC")
    assert out1 == b"\x00\x00\x01\x01"
    assert len(made) == 2  # one per distinct ssrc
    assert made[0].calls == [b"opusA", b"opusB"]
    assert made[1].calls == [b"opusC"]


def test_reset_drops_decoder():
    od = OpusDecoders(decoder_factory=lambda: FakeDecoder())
    od.decode(111, b"x")
    od.reset(111)
    assert 111 not in od._decoders
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_opus_decode.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/opus_decode.py
"""Per-SSRC Opus decoding to 48 kHz 16-bit stereo PCM, via py-cord's libopus binding."""


def _default_factory():
    import discord
    return discord.opus.Decoder()


class OpusDecoders:
    def __init__(self, decoder_factory=_default_factory):
        self._factory = decoder_factory
        self._decoders: dict[int, object] = {}

    def decode(self, ssrc: int, opus_bytes: bytes) -> bytes:
        dec = self._decoders.get(ssrc)
        if dec is None:
            dec = self._factory()
            self._decoders[ssrc] = dec
        return dec.decode(opus_bytes)

    def reset(self, ssrc: int) -> None:
        self._decoders.pop(ssrc, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_opus_decode.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add dave_voice/opus_decode.py tests/test_opus_decode.py
git commit -m "feat: per-SSRC Opus decoders"
```

> **LIVE VERIFICATION NOTE:** `discord.opus.Decoder().decode(data)` signature/return (some versions want `(data, frame_size)` or return a `memoryview`) is confirmed in Task 12. The injected-factory design keeps this unit test hermetic; the default factory is exercised live.

---

## Task 9: Voice WebSocket gateway (lifecycle + opcode routing)

**Files:**
- Create: `dave_voice/voice_ws.py`
- Test: `tests/test_voice_ws_routing.py`

**Interfaces:**
- Consumes: `dave_voice.opcodes` (VoiceOp, BINARY_SERVER_OPS, decode_binary, encode_binary), `dave_voice.mls.MLSManager`.
- Produces: `class VoiceGateway` driving the voice WS. Constructor:
  `__init__(self, *, endpoint, server_id, user_id, session_id, token, mls: MLSManager, on_ready, on_session_description, on_speaking)`.
  - `on_ready(ready_dict)` callback — fired on op 2 (carries ssrc, ip, port, modes).
  - `on_session_description(sd_dict)` — fired on op 4 (secret_key, mode, dave_protocol_version).
  - `on_speaking(user_id:int, ssrc:int)` — fired on op 5 to maintain SSRC↔user map.
  - `async def connect(self)`, `async def close(self)`.
  - `async def send_json(self, op:int, d:dict)`, `async def send_binary(self, op:int, payload:bytes)`.
  - Internal `async def _dispatch(self, message)` — the routing entry point (this is what the unit test drives directly, without a socket).
  - Tracks `self.last_seq` from binary frames for `seq_ack`.
  - Heartbeat loop sends `{"op":3,"d":{"t":<nonce>,"seq_ack":self.last_seq}}`.

  Routing rules in `_dispatch`:
  - JSON text frames → parse, switch on `op`. op 8 Hello → start heartbeat; op 2 → `on_ready`; op 4 → `on_session_description`; op 5 → `on_speaking`; op 6 → record ack; op 21 (prepare transition) → if `protocol_version==0` arrange passthrough then reply op 23 with the `transition_id` (immediate if `transition_id==0`); op 22 (execute) → fire `self.on_execute(transition_id)`; op 24 → `mls.on_prepare_epoch` and send any reply; op 31 path handled via mls; op 11/13 → ignore (handled by control plane).
  - Binary frames (first byte high enough / detected by caller passing `is_binary`) → `decode_binary`, update `self.last_seq`, switch on opcode: 25 → `mls.on_external_sender`; 27 → `mls.on_proposals` → send reply; 29 → parse `transition_id`+commit then `mls.on_announce_commit` → send reply; 30 → parse `transition_id`+welcome then `mls.on_welcome` → send reply.
  - For 29/30 the binary payload after the opcode is `[u16 transition_id][MLSMessage...]`; slice accordingly.

  Replies returned by MLSManager as `(opcode, payload)` are sent via `send_json` if the opcode is JSON-type (23, 31) else `send_binary`.

  The unit test injects a fake `send_json`/`send_binary` (records calls) and a fake MLSManager, and calls `_dispatch` with crafted messages — no real WebSocket.

- [ ] **Step 1: Write the failing test (routing only, socket-free)**

```python
# tests/test_voice_ws_routing.py
import json
import struct
import pytest
from dave_voice.voice_ws import VoiceGateway
from dave_voice.opcodes import VoiceOp


class FakeMLS:
    def __init__(self):
        self.calls = []

    def on_external_sender(self, pkg):
        self.calls.append(("ext", pkg))

    def on_prepare_epoch(self, version, epoch):
        self.calls.append(("epoch", version, epoch))
        return (VoiceOp.DAVE_MLS_KEY_PACKAGE, b"KP")

    def on_proposals(self, blob):
        self.calls.append(("proposals", blob))
        return (VoiceOp.DAVE_MLS_COMMIT_WELCOME, b"COMMIT")

    def on_announce_commit(self, tid, commit):
        self.calls.append(("announce", tid, commit))
        return (VoiceOp.DAVE_TRANSITION_READY, json.dumps({"transition_id": tid}).encode())

    def on_welcome(self, tid, welcome):
        self.calls.append(("welcome", tid, welcome))
        return (VoiceOp.DAVE_TRANSITION_READY, json.dumps({"transition_id": tid}).encode())


def make_gw():
    seen = {"ready": None, "sd": None, "speaking": [], "sent": []}
    gw = VoiceGateway(
        endpoint="x", server_id=1, user_id=2, session_id="s", token="t",
        mls=FakeMLS(),
        on_ready=lambda d: seen.__setitem__("ready", d),
        on_session_description=lambda d: seen.__setitem__("sd", d),
        on_speaking=lambda uid, ssrc: seen["speaking"].append((uid, ssrc)),
    )

    async def fake_json(op, d):
        seen["sent"].append(("json", op, d))

    async def fake_binary(op, payload):
        seen["sent"].append(("bin", op, payload))

    gw.send_json = fake_json
    gw.send_binary = fake_binary
    return gw, seen


async def test_ready_and_session_description_and_speaking():
    gw, seen = make_gw()
    await gw._dispatch(json.dumps({"op": 2, "d": {"ssrc": 9, "ip": "1.2.3.4", "port": 50, "modes": ["aead_aes256_gcm_rtpsize"]}}), is_binary=False)
    await gw._dispatch(json.dumps({"op": 4, "d": {"secret_key": [0], "mode": "aead_aes256_gcm_rtpsize", "dave_protocol_version": 1}}), is_binary=False)
    await gw._dispatch(json.dumps({"op": 5, "d": {"user_id": "77", "ssrc": 9}}), is_binary=False)
    assert seen["ready"]["ssrc"] == 9
    assert seen["sd"]["dave_protocol_version"] == 1
    assert seen["speaking"] == [(77, 9)]


async def test_binary_external_sender_updates_seq_and_calls_mls():
    gw, seen = make_gw()
    msg = struct.pack(">H", 12) + bytes([25]) + b"EXTPKG"
    await gw._dispatch(msg, is_binary=True)
    assert gw.last_seq == 12
    assert ("ext", b"EXTPKG") in gw.mls.calls


async def test_binary_proposals_sends_commit():
    gw, seen = make_gw()
    msg = struct.pack(">H", 13) + bytes([27]) + b"PROPS"
    await gw._dispatch(msg, is_binary=True)
    assert ("bin", VoiceOp.DAVE_MLS_COMMIT_WELCOME, b"COMMIT") in seen["sent"]


async def test_binary_welcome_parses_transition_id_and_replies_ready():
    gw, seen = make_gw()
    welcome_body = b"WELCOMEBYTES"
    msg = struct.pack(">H", 20) + bytes([30]) + struct.pack(">H", 5) + welcome_body
    await gw._dispatch(msg, is_binary=True)
    assert ("welcome", 5, welcome_body) in gw.mls.calls
    assert ("json", VoiceOp.DAVE_TRANSITION_READY, {"transition_id": 5}) in seen["sent"]


async def test_prepare_epoch_sends_key_package():
    gw, seen = make_gw()
    await gw._dispatch(json.dumps({"op": 24, "d": {"protocol_version": 1, "epoch": 1}}), is_binary=False)
    assert ("bin", VoiceOp.DAVE_MLS_KEY_PACKAGE, b"KP") in seen["sent"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_voice_ws_routing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dave_voice.voice_ws'`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/voice_ws.py
"""Voice WebSocket gateway v8: lifecycle, heartbeat+seq_ack, and opcode routing."""
import asyncio
import itertools
import json
import struct

import websockets

from dave_voice.opcodes import (
    VoiceOp,
    BINARY_SERVER_OPS,
    decode_binary,
    encode_binary,
)

# JSON-format client->server opcodes (sent as text even when produced by MLSManager).
_JSON_REPLY_OPS = {int(VoiceOp.DAVE_TRANSITION_READY), int(VoiceOp.DAVE_MLS_INVALID_COMMIT_WELCOME)}


class VoiceGateway:
    def __init__(self, *, endpoint, server_id, user_id, session_id, token,
                 mls, on_ready, on_session_description, on_speaking,
                 on_execute=None, on_prepare_passthrough=None):
        self.endpoint = endpoint
        self.server_id = server_id
        self.user_id = user_id
        self.session_id = session_id
        self.token = token
        self.mls = mls
        self.on_ready = on_ready
        self.on_session_description = on_session_description
        self.on_speaking = on_speaking
        self.on_execute = on_execute or (lambda tid: None)
        self.on_prepare_passthrough = on_prepare_passthrough or (lambda: None)
        self.ws = None
        self.last_seq = 0
        self._hb_nonce = itertools.count(1)
        self._heartbeat_task = None
        self._recv_task = None

    async def connect(self):
        url = f"wss://{self.endpoint}?v=8"
        self.ws = await websockets.connect(url, max_size=None)
        await self.send_json(VoiceOp.IDENTIFY, {
            "server_id": str(self.server_id),
            "user_id": str(self.user_id),
            "session_id": self.session_id,
            "token": self.token,
            "max_dave_protocol_version": __import__("dave").get_max_supported_protocol_version(),
        })
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def close(self):
        for t in (self._heartbeat_task, self._recv_task):
            if t:
                t.cancel()
        if self.ws:
            await self.ws.close()

    async def send_json(self, op, d):
        await self.ws.send(json.dumps({"op": int(op), "d": d}))

    async def send_binary(self, op, payload):
        await self.ws.send(encode_binary(int(op), payload))

    async def _recv_loop(self):
        try:
            async for message in self.ws:
                is_binary = isinstance(message, (bytes, bytearray))
                await self._dispatch(message, is_binary=is_binary)
        except asyncio.CancelledError:
            pass

    async def _heartbeat_loop(self, interval_ms):
        try:
            while True:
                await asyncio.sleep(interval_ms / 1000)
                await self.send_json(VoiceOp.HEARTBEAT, {
                    "t": next(self._hb_nonce),
                    "seq_ack": self.last_seq,
                })
        except asyncio.CancelledError:
            pass

    async def _send_reply(self, reply):
        if reply is None:
            return
        op, payload = reply
        if int(op) in _JSON_REPLY_OPS:
            await self.send_json(op, json.loads(payload.decode()))
        else:
            await self.send_binary(op, payload)

    async def _dispatch(self, message, is_binary):
        if is_binary:
            seq, opcode, payload = decode_binary(message)
            self.last_seq = seq
            if opcode == VoiceOp.DAVE_MLS_EXTERNAL_SENDER:
                self.mls.on_external_sender(payload)
            elif opcode == VoiceOp.DAVE_MLS_PROPOSALS:
                await self._send_reply(self.mls.on_proposals(payload))
            elif opcode == VoiceOp.DAVE_MLS_ANNOUNCE_COMMIT:
                tid = struct.unpack_from(">H", payload, 0)[0]
                await self._send_reply(self.mls.on_announce_commit(tid, payload[2:]))
            elif opcode == VoiceOp.DAVE_MLS_WELCOME:
                tid = struct.unpack_from(">H", payload, 0)[0]
                await self._send_reply(self.mls.on_welcome(tid, payload[2:]))
            return

        frame = json.loads(message)
        op = frame.get("op")
        d = frame.get("d") or {}
        if "seq" in frame and frame["seq"] is not None:
            self.last_seq = frame["seq"]

        if op == VoiceOp.HELLO:
            interval = d["heartbeat_interval"]
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(interval))
        elif op == VoiceOp.READY:
            self.on_ready(d)
        elif op == VoiceOp.SESSION_DESCRIPTION:
            self.on_session_description(d)
        elif op == VoiceOp.SPEAKING:
            self.on_speaking(int(d["user_id"]), int(d["ssrc"]))
        elif op == VoiceOp.DAVE_PREPARE_EPOCH:
            await self._send_reply(self.mls.on_prepare_epoch(d["protocol_version"], d["epoch"]))
        elif op == VoiceOp.DAVE_PREPARE_TRANSITION:
            tid = d.get("transition_id", 0)
            if d.get("protocol_version") == 0:
                self.on_prepare_passthrough()
            await self.send_json(VoiceOp.DAVE_TRANSITION_READY, {"transition_id": tid})
        elif op == VoiceOp.DAVE_EXECUTE_TRANSITION:
            self.on_execute(d.get("transition_id", 0))
        # op 6 (ack), 9 (resumed), 11/13 (clients connect/disconnect) -> no-op here
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_voice_ws_routing.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add dave_voice/voice_ws.py tests/test_voice_ws_routing.py
git commit -m "feat: voice WS gateway v8 lifecycle and opcode routing"
```

---

## Task 10: UDP receiver

**Files:**
- Create: `dave_voice/udp_receiver.py`
- Test: `tests/test_udp_receiver.py`

**Interfaces:**
- Produces:
  - `class VoiceUDPProtocol(asyncio.DatagramProtocol)` constructed with `__init__(self, on_packet)` where `on_packet(data: bytes) -> None` is called per datagram. `connection_made`/`datagram_received` standard overrides.
  - `async def open_udp(loop, remote_ip, remote_port, on_packet) -> tuple[transport, protocol]` — creates the endpoint and returns it.
  - `discover_ip(sock, ssrc, remote) -> tuple[str,int]` is **not** here (the WS owns IP discovery using `ip_discovery.py` over the same socket); this module is the steady-state receive path only.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_udp_receiver.py
from dave_voice.udp_receiver import VoiceUDPProtocol


def test_datagram_received_invokes_callback():
    got = []
    proto = VoiceUDPProtocol(on_packet=lambda data: got.append(data))
    proto.datagram_received(b"rtp-bytes", ("1.2.3.4", 5000))
    assert got == [b"rtp-bytes"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_udp_receiver.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/udp_receiver.py
"""asyncio UDP receive endpoint for RTP voice packets."""
import asyncio


class VoiceUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self.on_packet = on_packet
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.on_packet(data)

    def error_received(self, exc):
        print(f"udp error: {exc}", flush=True)


async def open_udp(loop, remote_ip, remote_port, on_packet):
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: VoiceUDPProtocol(on_packet),
        remote_addr=(remote_ip, remote_port),
    )
    return transport, protocol
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_udp_receiver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dave_voice/udp_receiver.py tests/test_udp_receiver.py
git commit -m "feat: asyncio UDP receiver for RTP voice"
```

---

## Task 11: DAVEVoiceClient orchestrator (assembly + decode chain)

**Files:**
- Create: `dave_voice/voice_client.py`
- Test: `tests/test_voice_client_decode.py`

**Interfaces:**
- Consumes: every prior `dave_voice` module.
- Produces: `class DAVEVoiceClient` that the control plane (`bot.py`) drives. Public API:
  - `__init__(self, *, server_id, channel_id, user_id, session_id, token, endpoint, on_pcm)` where `on_pcm(user_id:int, pcm:bytes)` is called with 48 kHz stereo PCM per decoded packet.
  - `async def start(self)` — opens WS, waits for op 2 + op 4, performs IP discovery + Select Protocol, builds `TransportCrypto`, opens UDP, begins receiving.
  - `async def stop(self)` — tears down WS + UDP.
  - The **decode chain** (the unit-tested core): `_handle_packet(self, data: bytes) -> None` →
    1. `parse_rtp_header` → get `ssrc`, strip RTP extension if `version_flags & 0x10`.
    2. `transport.decrypt(packet, header_len)` → media payload.
    3. look up `user_id` from `ssrc_to_user`; skip if unknown.
    4. `mls.decryptor_for(ssrc).decrypt(MediaType.audio, payload)` → plaintext Opus (or `None` if no key yet / passthrough handled internally).
    5. `opus.decode(ssrc, plaintext)` → PCM.
    6. `on_pcm(user_id, pcm)`.
  - The decode chain is factored so the test can drive `_handle_packet` with a fake transport, fake mls decryptor, and fake opus, asserting the right user/PCM comes out.

  The op-4 handler sets `dave_protocol_version`; if ≥1 the MLS path is active, else `mls` decryptors run in passthrough (call `transition_to_passthrough_mode(True)` on each). On op-2/op-4 also call `mls.set_group_id(channel_id)` and `mls.set_version(dave_protocol_version)`. After each `on_speaking`, call `mls.refresh_ratchets(self.ssrc_to_user)` so a newly-seen speaker's decryptor gets its ratchet. Also refresh on op-22 execute.

- [ ] **Step 1: Write the failing test (decode chain with injected fakes)**

```python
# tests/test_voice_client_decode.py
import struct
from dave_voice.voice_client import DAVEVoiceClient


class FakeTransport:
    def decrypt(self, packet, header_len=12):
        return b"CIPHER->" + packet[header_len:-4]


class FakeDecryptor:
    def decrypt(self, media_type, frame):
        return b"OPUS:" + frame


class FakeMLS:
    def __init__(self):
        self._d = FakeDecryptor()

    def decryptor_for(self, ssrc):
        return self._d

    def refresh_ratchets(self, mapping):
        pass


class FakeOpus:
    def decode(self, ssrc, opus_bytes):
        return b"PCM(" + opus_bytes + b")"


def test_handle_packet_full_chain_emits_pcm_for_known_user():
    emitted = []
    c = DAVEVoiceClient(
        server_id=1, channel_id=2, user_id=3, session_id="s", token="t",
        endpoint="e", on_pcm=lambda uid, pcm: emitted.append((uid, pcm)),
    )
    c.transport = FakeTransport()
    c.mls = FakeMLS()
    c.opus = FakeOpus()
    c.ssrc_to_user = {0x01020304: 77}

    header = struct.pack(">BBHII", 0x80, 0x78, 1, 2, 0x01020304)
    nonce4 = struct.pack(">I", 9)
    packet = header + b"frame-bytes" + nonce4

    c._handle_packet(packet)

    assert len(emitted) == 1
    uid, pcm = emitted[0]
    assert uid == 77
    # transport -> b"CIPHER->frame-bytes", decryptor -> b"OPUS:CIPHER->frame-bytes", opus -> PCM(...)
    assert pcm == b"PCM(OPUS:CIPHER->frame-bytes)"


def test_handle_packet_unknown_ssrc_is_dropped():
    emitted = []
    c = DAVEVoiceClient(
        server_id=1, channel_id=2, user_id=3, session_id="s", token="t",
        endpoint="e", on_pcm=lambda uid, pcm: emitted.append((uid, pcm)),
    )
    c.transport = FakeTransport()
    c.mls = FakeMLS()
    c.opus = FakeOpus()
    c.ssrc_to_user = {}
    header = struct.pack(">BBHII", 0x80, 0x78, 1, 2, 0xDEADBEEF)
    c._handle_packet(header + b"x" + struct.pack(">I", 1))
    assert emitted == []


def test_handle_packet_none_plaintext_is_dropped():
    emitted = []
    c = DAVEVoiceClient(
        server_id=1, channel_id=2, user_id=3, session_id="s", token="t",
        endpoint="e", on_pcm=lambda uid, pcm: emitted.append((uid, pcm)),
    )

    class NoneDecryptor:
        def decrypt(self, mt, frame):
            return None

    class NoneMLS:
        def decryptor_for(self, ssrc):
            return NoneDecryptor()

    c.transport = FakeTransport()
    c.mls = NoneMLS()
    c.opus = FakeOpus()
    c.ssrc_to_user = {0x01020304: 77}
    header = struct.pack(">BBHII", 0x80, 0x78, 1, 2, 0x01020304)
    c._handle_packet(header + b"x" + struct.pack(">I", 1))
    assert emitted == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_voice_client_decode.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# dave_voice/voice_client.py
"""Top-level DAVE voice receive client: assembles WS + UDP + transport + MLS + opus."""
import asyncio
import socket
import struct

import dave

from dave_voice import ip_discovery
from dave_voice.mls import MLSManager
from dave_voice.opus_decode import OpusDecoders
from dave_voice.rtp import parse_rtp_header, HEADER_LEN
from dave_voice.transport import TransportCrypto, SUPPORTED_MODES
from dave_voice.udp_receiver import open_udp
from dave_voice.voice_ws import VoiceGateway

_EXT_FLAG = 0x10  # version_flags bit indicating an RTP header extension


class DAVEVoiceClient:
    def __init__(self, *, server_id, channel_id, user_id, session_id, token,
                 endpoint, on_pcm):
        self.server_id = server_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.session_id = session_id
        self.token = token
        self.endpoint = endpoint.replace("wss://", "").replace(":443", "")
        self.on_pcm = on_pcm

        self.mls = MLSManager(self_user_id=user_id)
        self.mls.set_group_id(channel_id)
        self.opus = OpusDecoders()
        self.transport: TransportCrypto | None = None
        self.ssrc_to_user: dict[int, int] = {}
        self.dave_version = 0

        self._ssrc = None
        self._server_ip = None
        self._server_port = None
        self._modes = []
        self._udp_transport = None
        self._sock = None
        self._gw = None
        self._ready_evt = asyncio.Event()
        self._sd_evt = asyncio.Event()

    # ---- callbacks from the gateway ----
    def _on_ready(self, d):
        self._ssrc = d["ssrc"]
        self._server_ip = d["ip"]
        self._server_port = d["port"]
        self._modes = d["modes"]
        self._ready_evt.set()

    def _on_session_description(self, d):
        self.dave_version = d.get("dave_protocol_version", 0) or 0
        self.mls.set_version(self.dave_version if self.dave_version else 1)
        secret_key = bytes(d["secret_key"])
        mode = d.get("mode") or self._chosen_mode
        self.transport = TransportCrypto(mode, secret_key)
        self._sd_evt.set()

    def _on_speaking(self, user_id, ssrc):
        self.ssrc_to_user[ssrc] = user_id
        if self.dave_version >= 1:
            self.mls.refresh_ratchets(self.ssrc_to_user)

    def _on_execute(self, transition_id):
        if self.dave_version >= 1:
            self.mls.refresh_ratchets(self.ssrc_to_user)

    def _on_prepare_passthrough(self):
        for ssrc in list(self.mls.decryptors):
            self.mls.decryptor_for(ssrc).transition_to_passthrough_mode(True, 0.0)

    # ---- receive path (unit-tested core) ----
    def _handle_packet(self, data: bytes) -> None:
        if len(data) < HEADER_LEN + 4:
            return
        pkt = parse_rtp_header(data)
        header_len = HEADER_LEN
        if pkt.version_flags & _EXT_FLAG:
            # extension: [u16 profile][u16 length-in-32bit-words] then words
            ext_words = struct.unpack_from(">H", data, HEADER_LEN + 2)[0]
            header_len = HEADER_LEN + 4 + ext_words * 4
        payload = self.transport.decrypt(data, header_len)
        user_id = self.ssrc_to_user.get(pkt.ssrc)
        if user_id is None:
            return
        plaintext = self.mls.decryptor_for(pkt.ssrc).decrypt(dave.MediaType.audio, payload)
        if not plaintext:
            return
        pcm = self.opus.decode(pkt.ssrc, plaintext)
        if pcm:
            self.on_pcm(user_id, pcm)

    @property
    def _chosen_mode(self):
        for m in SUPPORTED_MODES:
            if m in self._modes:
                return m
        raise RuntimeError(f"no supported transport mode in {self._modes}")

    # ---- lifecycle ----
    async def start(self):
        self._gw = VoiceGateway(
            endpoint=self.endpoint, server_id=self.server_id, user_id=self.user_id,
            session_id=self.session_id, token=self.token, mls=self.mls,
            on_ready=self._on_ready, on_session_description=self._on_session_description,
            on_speaking=self._on_speaking, on_execute=self._on_execute,
            on_prepare_passthrough=self._on_prepare_passthrough,
        )
        await self._gw.connect()
        await self._ready_evt.wait()

        # IP discovery over a UDP socket, then keep it for receive.
        loop = asyncio.get_running_loop()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._sock.connect((self._server_ip, self._server_port))
        await loop.sock_sendall(self._sock, ip_discovery.build_request(self._ssrc))
        resp = await loop.sock_recv(self._sock, 74)
        my_ip, my_port = ip_discovery.parse_response(resp)

        await self._gw.send_json(1, {  # Select Protocol
            "protocol": "udp",
            "data": {"address": my_ip, "port": my_port, "mode": self._chosen_mode},
        })
        await self._sd_evt.wait()

        # Hand the connected socket to an asyncio datagram endpoint for receive.
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: __import__("dave_voice.udp_receiver", fromlist=["VoiceUDPProtocol"]).VoiceUDPProtocol(self._handle_packet),
            sock=self._sock,
        )

    async def stop(self):
        if self._udp_transport:
            self._udp_transport.close()
        if self._gw:
            await self._gw.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_voice_client_decode.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the full unit suite**

Run: `pytest -v`
Expected: all tests from Tasks 2–11 PASS.

- [ ] **Step 6: Commit**

```bash
git add dave_voice/voice_client.py tests/test_voice_client_decode.py
git commit -m "feat: DAVEVoiceClient orchestrator and receive decode chain"
```

> **LIVE CHECKPOINT (operator):** the live integration (`start()`) cannot be unit-tested. It is verified in Task 13 against a real DAVE call. Known live-verification points carried here: `set_external_sender` input framing, `init` group_id semantics, the exact RTP extension layout, and `discord.opus.Decoder.decode` signature.

---

## Task 12: Wire DAVEVoiceClient into `bot.py` (control plane)

**Files:**
- Modify: `bot.py`

**Interfaces:**
- Consumes: `dave_voice.voice_client.DAVEVoiceClient`; the existing `Meeting`, `store`, `flusher`, `pool`, DB-insert code (unchanged).
- Produces: `/join` and `/leave` that use the custom client instead of py-cord's `start_recording`. The PCM contract feeds the existing `store()` path verbatim.

Replace the py-cord voice path with: trigger op 4 via `guild.change_voice_state(channel=...)`, capture `VOICE_STATE_UPDATE`/`VOICE_SERVER_UPDATE` via py-cord events, start `DAVEVoiceClient`, and route `on_pcm` into the same silence-gap segmenter the old `LiveSink` fed.

- [ ] **Step 1: Add a PCM-buffering sink that mirrors the old LiveSink contract**

In `bot.py`, replace the `LiveSink(Sink)` class (it extended py-cord's `Sink`; we no longer use py-cord recording) with a plain buffer keyed by user id. Keep `drain_ready`/`drain_all` identical so `flusher`/`leave` are untouched:

```python
class PcmBuffer:
    """Accumulates per-user PCM emitted by DAVEVoiceClient; silence-gap segmented."""

    def __init__(self):
        self.lock = threading.Lock()
        self.buf: dict[int, bytearray] = defaultdict(bytearray)
        self.last: dict[int, float] = {}
        self.start: dict[int, float] = {}

    def write(self, user: int, data: bytes):
        now = time.monotonic()
        with self.lock:
            if not self.buf[user]:
                self.start[user] = now
            self.buf[user].extend(data)
            self.last[user] = now

    def _pop(self, only_silent: bool, silence_s: float = 0.0):
        now = time.monotonic()
        out = []
        with self.lock:
            for user in list(self.buf.keys()):
                b = self.buf[user]
                if not b:
                    continue
                if only_silent and (now - self.last.get(user, now)) < silence_s:
                    continue
                out.append((user, bytes(b), self.start.get(user, now), self.last.get(user, now)))
                self.buf[user] = bytearray()
        return out

    def drain_ready(self, silence_s):
        return self._pop(only_silent=True, silence_s=silence_s)

    def drain_all(self):
        return self._pop(only_silent=False)
```

Remove the now-unused `from discord.sinks import Sink` import.

- [ ] **Step 2: Add voice-server-update plumbing**

Add near the top-level state in `bot.py`:

```python
# guild_id -> asyncio.Future resolved with (session_id, token, endpoint)
_voice_server_waiters: dict[int, asyncio.Future] = {}
_voice_session_ids: dict[int, str] = {}


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id == bot.user.id and after.channel is not None:
        _voice_session_ids[after.channel.guild.id] = after.session_id


@bot.event
async def on_voice_server_update(data):
    # py-cord fires this raw event with {'guild_id', 'token', 'endpoint'}
    gid = int(data["guild_id"])
    fut = _voice_server_waiters.get(gid)
    if fut and not fut.done():
        fut.set_result((data["token"], data["endpoint"]))
```

> **LIVE VERIFICATION NOTE:** the exact py-cord event name/signature for the raw voice server update (`on_voice_server_update(data)` vs a `socket_response` hook) is confirmed in Task 13. If py-cord does not expose it directly, fall back to `@bot.listen()` on the raw `VOICE_SERVER_UPDATE` dispatch via `bot.on_socket_response`. The waiter/Future contract stays the same.

- [ ] **Step 3: Rewrite `/join`**

```python
@bot.slash_command(description="Join your voice channel and start transcribing")
async def join(ctx: discord.ApplicationContext):
    if not ctx.author.voice:
        await ctx.respond("Join a voice channel first.", ephemeral=True)
        return
    if ctx.guild.id in active:
        await ctx.respond("Already recording in this server.", ephemeral=True)
        return

    channel = ctx.author.voice.channel
    await ctx.defer()

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _voice_server_waiters[ctx.guild.id] = fut
    await ctx.guild.change_voice_state(channel=channel)  # sends gateway op 4
    token, endpoint = await asyncio.wait_for(fut, timeout=15)
    session_id = _voice_session_ids[ctx.guild.id]

    t0 = time.monotonic()
    session_uid = str(uuid.uuid4())
    async with pool.acquire() as c:
        meeting_id = await c.fetchval(
            "INSERT INTO meetings"
            " (user_id, platform, platform_specific_id, status, start_time, data, created_at, updated_at)"
            " VALUES ($1,'discord',$2,'active', now(), '{}'::jsonb, now(), now()) RETURNING id",
            VEXA_USER_ID, str(channel.id),
        )
        await c.execute(
            "INSERT INTO meeting_sessions (meeting_id, session_uid, session_start_time)"
            " VALUES ($1,$2, now())",
            meeting_id, session_uid,
        )

    meeting = Meeting(ctx.guild, channel, meeting_id, session_uid, t0)
    sink = PcmBuffer()

    client = DAVEVoiceClient(
        server_id=ctx.guild.id, channel_id=channel.id, user_id=bot.user.id,
        session_id=session_id, token=token, endpoint=endpoint,
        on_pcm=lambda uid, pcm: sink.write(uid, pcm),
    )
    await client.start()
    task = asyncio.create_task(flusher(ctx.guild.id))
    active[ctx.guild.id] = (client, sink, meeting, task)
    await ctx.respond(f"Recording **{channel.name}** → meeting `{meeting_id}`. Speakers tagged by name.")
```

- [ ] **Step 4: Update `/leave` and `flusher`/`active` types**

`flusher` already unpacks `_, sink, meeting, _ = entry` — unchanged. Update `/leave`:

```python
@bot.slash_command(description="Stop transcribing and leave the channel")
async def leave(ctx: discord.ApplicationContext):
    entry = active.pop(ctx.guild.id, None)
    if not entry:
        await ctx.respond("Not recording here.", ephemeral=True)
        return
    client, sink, meeting, task = entry
    task.cancel()
    await ctx.defer()
    await client.stop()
    await ctx.guild.change_voice_state(channel=None)  # leave the channel
    for uid, pcm, t0, t1 in sink.drain_all():
        await store(meeting, uid, pcm, t0, t1)
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE meetings SET status='completed', end_time=now(), updated_at=now() WHERE id=$1",
            meeting.id,
        )
    await ctx.respond(f"Stopped. meeting `{meeting.id}` saved.")
```

Update the type hint and imports at top of `bot.py`:

```python
from dave_voice.voice_client import DAVEVoiceClient
# active: guild_id -> (client, sink, meeting, flusher_task)
active: dict[int, tuple] = {}
```

Remove `import audioop`? No — `to_mono_wav` still uses it. Keep it.

- [ ] **Step 5: Verify the module imports cleanly (no live connection)**

Run:
```bash
python -c "import ast,sys; ast.parse(open('bot.py').read()); print('bot.py parses')"
DISCORD_TOKEN=x DATABASE_URL=postgresql://u:p@h/db VEXA_USER_ID=1 python -c "import dave_voice.voice_client; print('voice_client imports')"
```
Expected: both print success. (Do **not** run `bot.py` itself — it calls `bot.run` and needs a live token.)

- [ ] **Step 6: Commit**

```bash
git add bot.py
git commit -m "feat: wire DAVEVoiceClient into bot.py control plane; drop py-cord recording"
```

---

## Task 13: LIVE CHECKPOINT — gateway, MLS, decrypt, end-to-end (operator-run)

**Files:**
- Create: `scripts/live_smoke.py` (a standalone gateway-only smoke test for Milestone 2/4 before running the full bot)
- Modify (as needed): `dave_voice/*` to fix any live-only discrepancies the checkpoints surface.

**Interfaces:**
- Consumes: a real `DISCORD_TOKEN`, a guild where the bot is present, and a DAVE-enforced voice channel with at least one human speaker.
- Produces: verified end-to-end transcription rows.

This task is run by the operator in their environment; each step is a verification gate, not a code-generation step. Fix-forward into the relevant module when a gate fails.

- [ ] **Step 1: Milestone 2 — gateway reaches op 4 with DAVE active**

Add `scripts/live_smoke.py` that connects only the gateway and prints op 2 and op 4 payloads (reuse `DAVEVoiceClient.start()` but with `on_pcm=print`-noop, and add temporary `print` in `_on_session_description`). Run the bot's `/join` against a DAVE channel.
Expected: logs show op 8 Hello, heartbeats ACK'd (op 6), op 2 Ready with `ssrc`/`ip`/`port`/`modes`, and **op 4 Session Description with `dave_protocol_version: 1`**.
If `dave_protocol_version` is `0` or missing: confirm `max_dave_protocol_version` is in the Identify payload (Task 9) and that the channel is actually E2EE-enforced.

- [ ] **Step 2: Milestone 4 — MLS group establishes**

With logging in `MLSManager`, confirm the op 24 (epoch=1) → op 26 (key package) → op 27/28 or op 29/30 → op 23 dance completes and `session.has_established_group()` becomes `True`. Confirm `set_external_sender` accepted the op-25 payload without raising (this validates the input framing flagged in Task 7). If it raises, log the raw op-25 bytes and adjust the slice handed to `set_external_sender` (the payload after the opcode byte; the credential/signature framing per the protocol doc).

- [ ] **Step 3: Milestone 5 — first real decrypt to WAV**

In `_handle_packet`, temporarily tee decoded PCM for one SSRC to a WAV file (reuse `to_mono_wav` from `bot.py` or `wave` directly). Have a human speak. Confirm `decryptor.decrypt(...)` returns non-`None` for that speaker after `refresh_ratchets`, and the dumped WAV is intelligible speech.
If `decrypt` returns `None`: confirm `get_key_ratchet(str(user_id))` returns non-`None` (group established + correct user id), and that `refresh_ratchets` ran after the speaker's op-5 Speaking event.

- [ ] **Step 4: Milestone 6 — rows land in `transcriptions`**

Run the full bot. Speak a few sentences in the channel, then `/leave`. Query Postgres:
```sql
SELECT speaker, text, start_time, end_time FROM transcriptions
WHERE meeting_id = <the id from /join> ORDER BY start_time;
```
Expected: speaker-tagged rows with correct display names and intelligible transcripts.

- [ ] **Step 5: Milestone 7 — robustness pass**

Verify: a second person joining/leaving mid-call (new epoch → op 24/27/29 → ratchets refresh, audio keeps decrypting); a non-DAVE downgrade (op 21 protocol_version 0 → passthrough) if reproducible; reconnect/resume. Note any gaps as follow-up issues; these are ongoing-maintenance items per the spec's §7.

- [ ] **Step 6: Commit any fixes**

```bash
git add -A
git commit -m "fix: live-verified DAVE receive (gateway/MLS/decrypt adjustments)"
```

---

## Self-Review

**Spec coverage:**
- §0 (don't reimplement MLS/cipher; A vs B) — resolved: B killed by research; A de-risked via `dave.py`. Tasks 6/7 bind it. ✓
- §1 (voice gateway v8: VSU trigger, Identify+max_dave, Hello/heartbeat+seq_ack, Ready, IP discovery, Select Protocol, Session Description, transport vs DAVE layers, binary framing) — Tasks 2, 3, 5, 9, 11, 12. ✓
- §2 (MLS join flow, all opcodes 21–31, external sender, identity keypair, libdave responsibilities) — Tasks 7, 9 (routing), 13 (live). ✓
- §3 (frame layout, AES128-GCM, generation/nonce, SSRC→user, Opus decode→PCM) — handled by `dave.py` `Decryptor` (Task 6/7) + Tasks 8, 11. Frame-tail parsing is internal to `dave.py`, so no hand-rolled ULEB128 needed — documented in Global Constraints. ✓
- §4 (integration: keep segmenter/transcribe/inserts/slash commands; rip out LiveSink+start_recording; same tuples; SSRC→user replaces sink routing; name_for stays) — Task 12. ✓
- §5 (build/deps) — Task 1; `dave.py` wheel replaces the libdave/pybind11 build. ✓
- §6 milestones 1–7 — M1 done (research, this plan's preamble); M2–M7 = Task 13. M3 (test vectors) reframed as Task 6 binding smoke test. ✓
- §7 (risks/maintenance) — surfaced in Task 13 Step 5 and Global Constraints. ✓

**Placeholder scan:** no "TBD"/"handle edge cases"/"similar to Task N" left; live-only unknowns are explicitly marked as LIVE VERIFICATION NOTES with a concrete fallback, not silent gaps.

**Type consistency:** `MLSManager` methods return `(VoiceOp, bytes)` consistently; `VoiceGateway._send_reply` dispatches by `_JSON_REPLY_OPS`; `DAVEVoiceClient` uses `mls.decryptor_for(ssrc)`, `mls.refresh_ratchets(ssrc_to_user)`, `opus.decode(ssrc, bytes)`, `transport.decrypt(packet, header_len)` — all matching their defining tasks. `on_pcm(user_id, pcm)` is the single PCM contract from Task 11→12. `PcmBuffer.write(user, data)` matches `on_pcm`.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-20-dave-voice-receive.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best for Tasks 1–12 (the unit-testable build).
2. **Inline Execution** — execute tasks in this session with checkpoints for review.

Either way, **Task 13 must be run by you** (the operator) against a live DAVE channel — it's the only part that can't be tested without your Discord environment.

Which approach?
