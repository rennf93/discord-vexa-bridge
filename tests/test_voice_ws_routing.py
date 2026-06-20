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
