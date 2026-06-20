# dave_voice/voice_client.py
"""Top-level DAVE voice receive client: assembles WS + UDP + transport + MLS + opus."""
import asyncio
import socket
import struct

import dave

from dave_voice import ip_discovery
from dave_voice.mls import MLSManager
from dave_voice.opus_decode import OpusDecoders
from dave_voice.rtp import parse_rtp_header, HEADER_LEN
from dave_voice.transport import TransportCrypto, SUPPORTED_MODES
from dave_voice.udp_receiver import VoiceUDPProtocol
from dave_voice.voice_ws import VoiceGateway

_EXT_FLAG = 0x10  # version_flags bit indicating an RTP header extension


class DAVEVoiceClient:
    def __init__(self, *, server_id, channel_id, user_id, session_id, token,
                 endpoint, on_pcm):
        self.server_id = server_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.session_id = session_id
        self.token = token
        self.endpoint = endpoint.replace("wss://", "").replace(":443", "")
        self.on_pcm = on_pcm

        self.mls = MLSManager(self_user_id=user_id)
        self.mls.set_group_id(channel_id)
        self.opus = OpusDecoders()
        self.transport: TransportCrypto | None = None
        self.ssrc_to_user: dict[int, int] = {}
        self.dave_version = 0

        self._ssrc = None
        self._server_ip = None
        self._server_port = None
        self._modes = []
        self._udp_transport = None
        self._sock = None
        self._gw = None
        self._ready_evt = asyncio.Event()
        self._sd_evt = asyncio.Event()

    # ---- callbacks from the gateway ----
    def _on_ready(self, d):
        self._ssrc = d["ssrc"]
        self._server_ip = d["ip"]
        self._server_port = d["port"]
        self._modes = d["modes"]
        self._ready_evt.set()

    def _on_session_description(self, d):
        self.dave_version = d.get("dave_protocol_version", 0) or 0
        self.mls.set_version(self.dave_version if self.dave_version else 1)
        secret_key = bytes(d["secret_key"])
        mode = d.get("mode") or self._chosen_mode
        self.transport = TransportCrypto(mode, secret_key)
        self._sd_evt.set()

    def _on_speaking(self, user_id, ssrc):
        self.ssrc_to_user[ssrc] = user_id
        if self.dave_version >= 1:
            self.mls.refresh_ratchets(self.ssrc_to_user)

    def _on_execute(self, transition_id):
        if self.dave_version >= 1:
            self.mls.refresh_ratchets(self.ssrc_to_user)

    def _on_prepare_passthrough(self):
        for ssrc in list(self.mls.decryptors):
            self.mls.decryptor_for(ssrc).transition_to_passthrough_mode(True, 0.0)

    # ---- receive path (unit-tested core) ----
    def _handle_packet(self, data: bytes) -> None:
        if len(data) < HEADER_LEN + 4:
            return
        pkt = parse_rtp_header(data)
        header_len = HEADER_LEN
        if pkt.version_flags & _EXT_FLAG:
            # extension: [u16 profile][u16 length-in-32bit-words] then words
            ext_words = struct.unpack_from(">H", data, HEADER_LEN + 2)[0]
            header_len = HEADER_LEN + 4 + ext_words * 4
        payload = self.transport.decrypt(data, header_len)
        user_id = self.ssrc_to_user.get(pkt.ssrc)
        if user_id is None:
            return
        plaintext = self.mls.decryptor_for(pkt.ssrc).decrypt(dave.MediaType.audio, payload)
        if not plaintext:
            return
        pcm = self.opus.decode(pkt.ssrc, plaintext)
        if pcm:
            self.on_pcm(user_id, pcm)

    @property
    def _chosen_mode(self):
        for m in SUPPORTED_MODES:
            if m in self._modes:
                return m
        raise RuntimeError(f"no supported transport mode in {self._modes}")

    # ---- lifecycle ----
    async def start(self):
        self._gw = VoiceGateway(
            endpoint=self.endpoint, server_id=self.server_id, user_id=self.user_id,
            session_id=self.session_id, token=self.token, mls=self.mls,
            on_ready=self._on_ready, on_session_description=self._on_session_description,
            on_speaking=self._on_speaking, on_execute=self._on_execute,
            on_prepare_passthrough=self._on_prepare_passthrough,
        )
        await self._gw.connect()
        await self._ready_evt.wait()

        # IP discovery over a UDP socket, then keep it for receive.
        loop = asyncio.get_running_loop()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._sock.connect((self._server_ip, self._server_port))
        await loop.sock_sendall(self._sock, ip_discovery.build_request(self._ssrc))
        resp = await loop.sock_recv(self._sock, 74)
        my_ip, my_port = ip_discovery.parse_response(resp)

        await self._gw.send_json(1, {  # Select Protocol
            "protocol": "udp",
            "data": {"address": my_ip, "port": my_port, "mode": self._chosen_mode},
        })
        await self._sd_evt.wait()

        # Hand the connected socket to an asyncio datagram endpoint for receive.
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: VoiceUDPProtocol(self._handle_packet),
            sock=self._sock,
        )

    async def stop(self):
        if self._udp_transport:
            self._udp_transport.close()
        if self._gw:
            await self._gw.close()
