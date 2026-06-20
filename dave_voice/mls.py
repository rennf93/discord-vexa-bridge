# dave_voice/mls.py
"""MLS group orchestration over dave.py's Session + per-SSRC Decryptor registry.

Pure routing/state logic: methods return (opcode, payload_bytes) reply tuples or
None; the caller (VoiceGateway) does the actual network send. All RFC-9420 / frame
crypto lives inside dave.py.

NOTE: this module currently carries verbose [dave] diagnostics while we bring the
MLS receive handshake up live. They can be quieted once decryption is confirmed.
"""
import json

import dave

from dave_voice.opcodes import VoiceOp


def _d(msg: str) -> None:
    print(f"[dave] {msg}", flush=True)


class MLSManager:
    def __init__(self, self_user_id: int):
        self.self_user_id = self_user_id
        self.session = dave.Session(mls_failure_callback=self._on_mls_failure)
        self.decryptors: dict[int, dave.Decryptor] = {}
        self.recognized_user_ids: set[str] = set()
        self.version = 1
        self.group_id = 0
        self._initialized = False

    def _on_mls_failure(self, a: str, b: str) -> None:
        _d(f"MLS FAILURE callback: {a!r} / {b!r}")

    def _group_state(self) -> str:
        try:
            return f"established={self.session.has_established_group()}"
        except Exception as e:  # pragma: no cover - diagnostic only
            return f"established=? ({e})"

    def set_group_id(self, group_id: int) -> None:
        self.group_id = group_id

    def set_version(self, version: int) -> None:
        self.version = version
        if self._initialized:
            self.session.set_protocol_version(version)

    def initialize_group(self, version: int):
        """Create (or recreate) the local MLS group and return the op-26 key package.

        Per py-cord's reinit_dave_session (state.py): the group is created on op 4
        (Session Description) when dave_protocol_version > 0, and the client
        immediately publishes its key package so the gateway can add it.
        """
        self.version = version
        if self._initialized:
            self.session.reset()
        self.session.init(version, self.group_id, str(self.self_user_id))
        self._initialized = True
        kp = self.session.get_marshalled_key_package()
        _d(f"group initialized (version={version} group={self.group_id} {self._group_state()}); sending key package {len(kp)} bytes")
        return (VoiceOp.DAVE_MLS_KEY_PACKAGE, kp)

    def on_external_sender(self, package: bytes) -> None:
        _d(f"op25 external sender: {len(package)} bytes (group {self._group_state()})")
        try:
            self.session.set_external_sender(package)
        except Exception as e:
            _d(f"set_external_sender raised: {e!r}")

    def on_prepare_epoch(self, version: int, epoch: int):
        _d(f"op24 prepare_epoch: version={version} epoch={epoch} recognized={sorted(self.recognized_user_ids)}")
        self.version = version
        if epoch == 1:
            return self.initialize_group(version)
        # epoch > 1: protocol-version change of the existing group
        if self._initialized:
            self.session.set_protocol_version(version)
        return None

    def on_proposals(self, blob: bytes):
        _d(f"op27 proposals: {len(blob)} bytes, recognized={sorted(self.recognized_user_ids)}")
        commit = self.session.process_proposals(blob, self.recognized_user_ids)
        if commit is None:
            _d("process_proposals -> no commit")
            return None
        _d(f"process_proposals -> commit {len(commit)} bytes")
        return (VoiceOp.DAVE_MLS_COMMIT_WELCOME, commit)

    def _ready_or_invalid(self, transition_id: int, result):
        if isinstance(result, dave.RejectType):
            _d(f"transition {transition_id}: REJECT {result!r} -> invalid")
            return (
                VoiceOp.DAVE_MLS_INVALID_COMMIT_WELCOME,
                json.dumps({"transition_id": transition_id}).encode(),
            )
        _d(f"transition {transition_id}: accepted ({self._group_state()}) -> ready; roster={result!r}")
        return (
            VoiceOp.DAVE_TRANSITION_READY,
            json.dumps({"transition_id": transition_id}).encode(),
        )

    def on_announce_commit(self, transition_id: int, commit: bytes):
        _d(f"op29 announce_commit: tid={transition_id} commit={len(commit)} bytes")
        result = self.session.process_commit(commit)
        return self._ready_or_invalid(transition_id, result)

    def on_welcome(self, transition_id: int, welcome: bytes):
        _d(f"op30 welcome: tid={transition_id} welcome={len(welcome)} bytes recognized={sorted(self.recognized_user_ids)}")
        result = self.session.process_welcome(welcome, self.recognized_user_ids)
        if result is None:
            _d(f"process_welcome -> None ({self._group_state()}) -> invalid")
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
        if not ssrc_to_user:
            return
        got, missing = [], []
        for ssrc, user_id in ssrc_to_user.items():
            ratchet = self.session.get_key_ratchet(str(user_id))
            if ratchet is not None:
                self.decryptor_for(ssrc).transition_to_key_ratchet(ratchet)
                got.append(user_id)
            else:
                missing.append(user_id)
        _d(f"refresh_ratchets ({self._group_state()}): keyed={got} no_ratchet={missing}")
