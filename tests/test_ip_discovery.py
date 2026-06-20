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
