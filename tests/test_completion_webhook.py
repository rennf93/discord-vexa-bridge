"""Tests for completion_webhook.py: the meeting.completed envelope this bridge emits.

Root cause this exists: the bridge writes Discord meetings straight into Vexa's Postgres,
so Vexa's own webhook emitter never sees them and its meeting.completed event never fires.
The obsidian-vexa-bridge receiver expects Vexa's exact webhook.v1 envelope and signature,
so these tests pin the contract against an independent HMAC computation, not just "does
emit() call our own sign()".
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

import completion_webhook as cw


def test_build_envelope_shape():
    start = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 27, 10, 30, 0, tzinfo=UTC)
    now = datetime(2026, 8, 27, 10, 30, 5, tzinfo=UTC)
    envelope = cw.build_envelope(42, "123456789012345678", start, end, now=now)

    assert envelope["event_type"] == "meeting.completed"
    assert envelope["api_version"] == "2026-03-01"
    assert envelope["created_at"] == "2026-08-27T10:30:05+00:00".replace("+00:00", "Z")
    meeting = envelope["data"]["meeting"]
    assert meeting == {
        "id": 42,
        "platform": "discord",
        "native_meeting_id": "123456789012345678",
        "status": "completed",
        "completion_reason": "stopped",
        "start_time": "2026-08-27T10:00:00Z",
        "end_time": "2026-08-27T10:30:00Z",
        "source": "discord-vexa-bridge",
    }
    assert envelope["event_id"].startswith("evt_")
    assert len(envelope["event_id"]) == len("evt_") + 32


def test_build_envelope_tolerates_null_end_time():
    """end_time is nullable in Vexa's schema; a still-open row must not raise. The receiver
    treats a missing end_time as optional, so the key is omitted rather than sent as null."""
    start = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
    envelope = cw.build_envelope(42, "123456789012345678", start, None)

    meeting = envelope["data"]["meeting"]
    assert meeting["start_time"] == "2026-08-27T10:00:00Z"
    assert "end_time" not in meeting


def test_build_envelope_tolerates_null_start_time():
    """start_time is nullable too; fall back to end_time so the envelope still has a marker."""
    end = datetime(2026, 8, 27, 10, 30, 0, tzinfo=UTC)
    envelope = cw.build_envelope(42, "123456789012345678", None, end)

    meeting = envelope["data"]["meeting"]
    assert meeting["start_time"] == "2026-08-27T10:30:00Z"
    assert meeting["end_time"] == "2026-08-27T10:30:00Z"


def test_build_envelope_falls_back_to_now_when_both_timestamps_null():
    now = datetime(2026, 8, 27, 11, 0, 0, tzinfo=UTC)
    envelope = cw.build_envelope(42, "123456789012345678", None, None, now=now)

    meeting = envelope["data"]["meeting"]
    assert meeting["start_time"] == "2026-08-27T11:00:00Z"
    assert "end_time" not in meeting


def test_event_id_is_deterministic_per_meeting():
    start = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 27, 10, 30, 0, tzinfo=UTC)

    e1 = cw.build_envelope(42, "chan-1", start, end)
    e2 = cw.build_envelope(42, "chan-1", start, end)
    assert e1["event_id"] == e2["event_id"]

    expected = "evt_" + hashlib.sha256(b"discord-vexa-bridge|42|completed").hexdigest()[:32]
    assert e1["event_id"] == expected


def test_event_id_differs_by_meeting_id():
    start = datetime(2026, 8, 27, 10, 0, 0, tzinfo=UTC)
    end = datetime(2026, 8, 27, 10, 30, 0, tzinfo=UTC)
    e1 = cw.build_envelope(1, "chan-1", start, end)
    e2 = cw.build_envelope(2, "chan-1", start, end)
    assert e1["event_id"] != e2["event_id"]


def test_sign_matches_independent_hmac_computation():
    secret = "s3cr3t"
    timestamp = "1755000000"
    body = b'{"event_type":"meeting.completed"}'

    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert cw.sign(secret, timestamp, body) == expected


def test_headers_for_carries_all_three_headers():
    secret = "s3cr3t"
    timestamp = "1755000000"
    body = b"{}"
    headers = cw.headers_for(secret, timestamp, body)

    assert headers["X-Webhook-Timestamp"] == timestamp
    assert headers["X-Webhook-Signature"] == cw.sign(secret, timestamp, body)
    assert headers["Authorization"] == f"Bearer {secret}"


async def test_emit_returns_true_on_200_with_signed_body():
    calls = []

    async def fake_post(url, headers, body):
        calls.append((url, headers, body))
        return 200

    envelope = {"event_type": "meeting.completed", "data": {"meeting": {"id": 1}}}
    ok = await cw.emit("https://example.test/hook", "s3cr3t", envelope, post=fake_post)

    assert ok is True
    assert len(calls) == 1
    url, headers, body = calls[0]
    assert url == "https://example.test/hook"
    assert body == json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()

    expected_sig = (
        "sha256="
        + hmac.new(b"s3cr3t", headers["X-Webhook-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
    )
    assert headers["X-Webhook-Signature"] == expected_sig


async def test_emit_retries_then_gives_up_on_persistent_500():
    calls = []

    async def fake_post(url, headers, body):
        calls.append(1)
        return 500

    ok = await cw.emit("https://example.test/hook", "s3cr3t", {"a": 1}, attempts=3, backoff_seconds=0, post=fake_post)

    assert ok is False
    assert len(calls) == 3


async def test_emit_survives_exception_from_post_seam():
    calls = []

    async def flaky_post(url, headers, body):
        calls.append(1)
        raise TimeoutError("connect timed out")

    ok = await cw.emit("https://example.test/hook", "s3cr3t", {"a": 1}, attempts=2, backoff_seconds=0, post=flaky_post)

    assert ok is False
    assert len(calls) == 2


async def test_emit_succeeds_after_a_transient_failure():
    calls = []

    async def eventually_ok(url, headers, body):
        calls.append(1)
        if len(calls) < 2:
            raise ConnectionError("refused")
        return 204

    ok = await cw.emit(
        "https://example.test/hook", "s3cr3t", {"a": 1}, attempts=3, backoff_seconds=0, post=eventually_ok
    )

    assert ok is True
    assert len(calls) == 2


@pytest.mark.parametrize("status", [100, 300, 404, 500, 599])
async def test_emit_treats_only_2xx_as_delivered(status):
    async def fake_post(url, headers, body):
        return status

    ok = await cw.emit("https://example.test/hook", "s3cr3t", {"a": 1}, attempts=1, backoff_seconds=0, post=fake_post)
    assert ok is False
