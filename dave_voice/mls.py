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
