"""Emit Vexa's webhook.v1 `meeting.completed` envelope when the bridge finalizes a meeting.

This bridge writes Discord meetings straight into Vexa's Postgres (see `_finalize_completed`
in bot.py), so Vexa's own webhook emitter never sees them and `meeting.completed` never
fires for a Discord call. The obsidian-vexa-bridge gained a webhook receiver that accepts
Vexa's exact webhook.v1 envelope and HMAC signature, so it can process a meeting on the
event instead of polling — this module reproduces that contract byte-for-byte (envelope
shape, signing, headers), verified against Vexa's own emitter source.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

API_VERSION = "2026-03-01"

# The seam emit() posts through: (url, headers, body) -> HTTP status code.
Poster = Callable[[str, dict[str, str], bytes], Awaitable[int]]


def _iso_z(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC with a trailing Z, Vexa's envelope timestamp format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_envelope(
    meeting_id: int,
    native_meeting_id: str,
    start_time: datetime,
    end_time: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the webhook.v1 envelope for a completed Discord meeting.

    `event_id` is derived only from `meeting_id` (never from `now`), so a redelivered
    completion for the same meeting produces the same event_id and the receiver can dedup.
    """
    created_at = now if now is not None else datetime.now(UTC)
    event_id = "evt_" + hashlib.sha256(f"discord-vexa-bridge|{meeting_id}|completed".encode()).hexdigest()[:32]
    return {
        "event_id": event_id,
        "event_type": "meeting.completed",
        "api_version": API_VERSION,
        "created_at": _iso_z(created_at),
        "data": {
            "meeting": {
                "id": meeting_id,
                "platform": "discord",
                "native_meeting_id": native_meeting_id,
                "status": "completed",
                "completion_reason": "stopped",
                "start_time": _iso_z(start_time),
                "end_time": _iso_z(end_time),
                "source": "discord-vexa-bridge",
            }
        },
    }


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over `timestamp + "." + body` — the exact check the receiver runs."""
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def headers_for(secret: str, timestamp: str, body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": sign(secret, timestamp, body),
        "Authorization": f"Bearer {secret}",
    }


async def _post_via_aiohttp(url: str, headers: dict[str, str], body: bytes) -> int:
    import aiohttp  # lazy: only the default seam needs it; tests inject their own `post`

    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession() as session, session.post(url, headers=headers, data=body, timeout=timeout) as r:
        return r.status


async def emit(
    url: str,
    secret: str,
    envelope: dict[str, Any],
    *,
    attempts: int = 3,
    backoff_seconds: float = 2.0,
    post: Poster | None = None,
) -> bool:
    """POST the signed envelope; retry on non-2xx or exceptions; never raise.

    The envelope is serialized exactly once and those bytes are both signed and sent, so
    the receiver's signature check (recomputed over the raw body it received) always matches.
    """
    poster = post or _post_via_aiohttp
    body = json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
    timestamp = str(int(time.time()))
    headers = headers_for(secret, timestamp, body)

    for attempt in range(1, attempts + 1):
        try:
            status = await poster(url, headers, body)
            if 200 <= status < 300:
                return True
            print(f"completion webhook HTTP {status} (attempt {attempt}/{attempts})", flush=True)
        except Exception as e:
            print(f"completion webhook error (attempt {attempt}/{attempts}): {e}", flush=True)
        if attempt < attempts:
            await asyncio.sleep(backoff_seconds)
    return False
