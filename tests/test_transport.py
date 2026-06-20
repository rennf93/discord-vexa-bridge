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
