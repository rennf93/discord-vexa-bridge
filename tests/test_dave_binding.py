# tests/test_dave_binding.py
import dave


def test_max_version_at_least_1():
    assert dave.get_max_supported_protocol_version() >= 1


def test_media_type_and_codec_enums():
    assert int(dave.MediaType.audio) == 0
    assert hasattr(dave.Codec, "opus")


def test_signature_keypair_generates():
    kp = dave.SignatureKeyPair.generate(dave.get_max_supported_protocol_version())
    assert kp is not None


def test_session_constructs_and_reports_no_group():
    s = dave.Session()
    assert s.has_established_group() is False


def test_decryptor_constructs_and_passthrough_decrypt_is_callable():
    d = dave.Decryptor()
    # In passthrough mode an un-encrypted frame should pass through unchanged.
    d.transition_to_passthrough_mode(True, 0.0)
    out = d.decrypt(dave.MediaType.audio, b"\x01\x02\x03")
    assert out == b"\x01\x02\x03"
