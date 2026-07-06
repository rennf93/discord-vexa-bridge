"""Tests for summarizer.vexa — read Vexa meetings/transcripts, write notes. HTTP faked.

The Vexa api-gateway shapes assumed here (verify against the live API on first real run):
- GET /meetings -> {"meetings":[{id, platform, platform_specific_id, start_time, end_time, status}, ...]}
  (a bare list is also accepted)
- GET /transcripts/{platform}/{native_meeting_id} -> {"transcripts":[{speaker, start_time, end_time, text}, ...]}
- PATCH /meetings/{platform}/{native_meeting_id} body {"data":{"notes": <md>}}
start_time / end_time are epoch seconds (float). X-API-Key header auth.
"""

from datetime import UTC

import pytest

from summarizer import config, vexa


def _cfg(url="http://vexa:8056", key="vexa-key"):
    return config.Config(vexa_api_url=url, vexa_api_key=key)


async def test_list_completed_meetings_filters_status_and_platform(monkeypatch):
    payload = {
        "meetings": [
            {
                "id": 1,
                "platform": "discord",
                "platform_specific_id": "d1",
                "start_time": 1000.0,
                "end_time": 1100.0,
                "status": "completed",
            },
            {
                "id": 2,
                "platform": "discord",
                "platform_specific_id": "d2",
                "start_time": 2000.0,
                "end_time": 2100.0,
                "status": "recording",
            },  # dropped
            {
                "id": 3,
                "platform": "zoom",
                "platform_specific_id": "z1",
                "start_time": 3000.0,
                "end_time": 3100.0,
                "status": "completed",
            },  # dropped (not in platforms)
            {
                "id": 4,
                "platform": "google_meet",
                "platform_specific_id": "g1",
                "start_time": 4000.0,
                "end_time": 4100.0,
                "status": "completed",
            },
        ]
    }

    async def fake_get(url, headers):
        assert url == "http://vexa:8056/meetings"
        assert headers["X-API-Key"] == "vexa-key"
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord", "google_meet"])
    ids = [m.id for m in meetings]
    assert ids == [1, 4]
    m1 = meetings[0]
    assert m1.platform == "discord"
    assert m1.native_meeting_id == "d1"
    assert m1.start.tzinfo == UTC
    assert m1.start.timestamp() == 1000.0


async def test_list_accepts_bare_list_response(monkeypatch):
    async def fake_get(url, headers):
        return 200, [
            {
                "id": 9,
                "platform": "discord",
                "platform_specific_id": "d9",
                "start_time": 1.0,
                "end_time": 2.0,
                "status": "completed",
            }
        ]

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord"])
    assert [m.id for m in meetings] == [9]


async def test_list_falls_back_to_per_meeting_detail_when_native_id_missing(monkeypatch):
    """If the list response omits platform_specific_id, fetch GET /meetings/{id} for it."""
    list_payload = {
        "meetings": [{"id": 1, "platform": "discord", "start_time": 1000.0, "end_time": 1100.0, "status": "completed"}]
    }

    calls = []

    async def fake_get(url, headers):
        calls.append(url)
        if url.endswith("/meetings"):
            return 200, list_payload
        if url.endswith("/meetings/1"):
            return 200, {
                "id": 1,
                "platform": "discord",
                "platform_specific_id": "d1",
                "start_time": 1000.0,
                "end_time": 1100.0,
                "status": "completed",
            }
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    meetings = await vexa.list_completed_meetings(_cfg(), ["discord"])
    assert meetings[0].native_meeting_id == "d1"
    assert "/meetings/1" in calls[-1]


async def test_get_transcript_orders_by_start_time(monkeypatch):
    payload = {
        "transcripts": [
            {"speaker": "Renzo", "start_time": 1050.0, "end_time": 1060.0, "text": "second"},
            {"speaker": "David", "start_time": 1000.0, "end_time": 1010.0, "text": "first"},
            {"speaker": "Renzo", "start_time": 1020.0, "end_time": 1030.0, "text": "middle"},
        ]
    }

    async def fake_get(url, headers):
        assert url == "http://vexa:8056/transcripts/discord/d1"
        return 200, payload

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1000.0, tz=UTC),
        end=_dt.fromtimestamp(1100.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert [u.text for u in utts] == ["first", "middle", "second"]
    assert utts[0].speaker == "David"


async def test_get_transcript_accepts_bare_list(monkeypatch):
    async def fake_get(url, headers):
        return 200, [{"speaker": "Renzo", "start_time": 1.0, "end_time": 2.0, "text": "hi"}]

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    utts = await vexa.get_transcript(_cfg(), m)
    assert len(utts) == 1 and utts[0].text == "hi"


async def test_write_notes_patches_with_data_notes(monkeypatch):
    captured = {}

    async def fake_patch(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, {"ok": True}

    monkeypatch.setattr(vexa, "_http_patch_json", fake_patch)
    from datetime import datetime as _dt

    from summarizer.types import Meeting

    m = Meeting(
        id=1,
        platform="discord",
        native_meeting_id="d1",
        start=_dt.fromtimestamp(1.0, tz=UTC),
        end=_dt.fromtimestamp(2.0, tz=UTC),
    )
    await vexa.write_notes(_cfg(), m, "# TL;DR\nstuff")
    assert captured["url"] == "http://vexa:8056/meetings/discord/d1"
    assert captured["headers"]["X-API-Key"] == "vexa-key"
    assert captured["body"] == {"data": {"notes": "# TL;DR\nstuff"}}


async def test_list_5xx_raises(monkeypatch):
    async def fake_get(url, headers):
        return 500, "err"

    monkeypatch.setattr(vexa, "_http_get_json", fake_get)
    with pytest.raises(vexa.VexaError):
        await vexa.list_completed_meetings(_cfg(), ["discord"])
