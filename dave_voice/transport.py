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
