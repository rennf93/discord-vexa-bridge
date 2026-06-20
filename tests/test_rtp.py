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
