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
