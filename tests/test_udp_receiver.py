# tests/test_udp_receiver.py
from dave_voice.udp_receiver import VoiceUDPProtocol


def test_datagram_received_invokes_callback():
    got = []
    proto = VoiceUDPProtocol(on_packet=lambda data: got.append(data))
    proto.datagram_received(b"rtp-bytes", ("1.2.3.4", 5000))
    assert got == [b"rtp-bytes"]
