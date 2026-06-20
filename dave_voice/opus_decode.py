"""Per-SSRC Opus decoding to 48 kHz 16-bit stereo PCM, via py-cord's libopus binding."""


def _default_factory():
    import discord
    return discord.opus.Decoder()


class OpusDecoders:
    def __init__(self, decoder_factory=_default_factory):
        self._factory = decoder_factory
        self._decoders: dict[int, object] = {}

    def decode(self, ssrc: int, opus_bytes: bytes) -> bytes:
        dec = self._decoders.get(ssrc)
        if dec is None:
            dec = self._factory()
            self._decoders[ssrc] = dec
        return dec.decode(opus_bytes)

    def reset(self, ssrc: int) -> None:
        self._decoders.pop(ssrc, None)
