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
