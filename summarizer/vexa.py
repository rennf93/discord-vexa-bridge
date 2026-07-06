"""Reads Vexa (meetings + transcripts) and writes meeting notes.

Read-only except write_notes (only fired when VEXA_NOTES_ENABLED). X-API-Key header auth.
start_time/end_time are epoch-second floats -> UTC datetimes. The list endpoint may omit
platform_specific_id (the native meeting id); when it does, fall back to GET /meetings/{id}.

HTTP is split into three async seams (_http_get_json / _http_patch_json) so tests fake them
without aiohttp. Response shapes are documented in tests/test_vexa.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summarizer.config import Config
    from summarizer.types import Meeting, Utterance


class VexaError(RuntimeError):
    """Raised on Vexa HTTP failures / non-200s."""


def _epoch_to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=UTC)


def _native_id(rec: dict[str, Any]) -> str | None:
    return rec.get("platform_specific_id") or rec.get("native_meeting_id")


async def list_completed_meetings(cfg: Config, platforms: list[str]) -> list[Meeting]:
    status, data = await _http_get_json(f"{cfg.vexa_api_url}/meetings", _headers(cfg))
    if status != 200:
        raise VexaError(f"GET /meetings -> HTTP {status}")
    rows = data["meetings"] if isinstance(data, dict) and "meetings" in data else data
    if not isinstance(rows, list):
        raise VexaError(f"unexpected /meetings shape: {type(data).__name__}")
    platforms_set = set(platforms)

    from summarizer.types import Meeting

    out: list[Meeting] = []
    for rec in rows:
        if not isinstance(rec, dict):
            continue
        if rec.get("status") != "completed":
            continue
        if rec.get("platform") not in platforms_set:
            continue
        native = _native_id(rec)
        if not native:
            # List response omitted the native id — fetch the detail record.
            dstatus, detail = await _http_get_json(f"{cfg.vexa_api_url}/meetings/{rec['id']}", _headers(cfg))
            if dstatus != 200 or not isinstance(detail, dict):
                raise VexaError(f"GET /meetings/{rec['id']} -> HTTP {dstatus}")
            native = _native_id(detail)
            if not native:
                raise VexaError(f"meeting {rec['id']} has no native_meeting_id")
            rec = {**rec, **detail}
        out.append(
            Meeting(
                id=int(rec["id"]),
                platform=str(rec["platform"]),
                native_meeting_id=str(native),
                start=_epoch_to_dt(rec["start_time"]),
                end=_epoch_to_dt(rec.get("end_time") or rec["start_time"]),
            )
        )
    return out


async def get_transcript(cfg: Config, meeting: Meeting) -> list[Utterance]:
    url = f"{cfg.vexa_api_url}/transcripts/{meeting.platform}/{meeting.native_meeting_id}"
    status, data = await _http_get_json(url, _headers(cfg))
    if status != 200:
        raise VexaError(f"GET transcripts -> HTTP {status}")
    rows = data["transcripts"] if isinstance(data, dict) and "transcripts" in data else data
    if not isinstance(rows, list):
        raise VexaError(f"unexpected transcripts shape: {type(data).__name__}")

    from summarizer.types import Utterance

    utts = [
        Utterance(
            speaker=str(r.get("speaker") or r.get("speaker_name") or "Unknown"),
            start_time=float(r.get("start_time", 0.0)),
            end_time=float(r.get("end_time", 0.0)),
            text=str(r.get("text") or r.get("transcript_text") or ""),
        )
        for r in rows
        if isinstance(r, dict)
    ]
    utts.sort(key=lambda u: u.start_time)
    return utts


async def write_notes(cfg: Config, meeting: Meeting, markdown: str) -> None:
    url = f"{cfg.vexa_api_url}/meetings/{meeting.platform}/{meeting.native_meeting_id}"
    status, data = await _http_patch_json(url, _headers(cfg), {"data": {"notes": markdown}})
    if status not in (200, 204):
        raise VexaError(f"PATCH meetings -> HTTP {status}: {str(data)[:200]}")


def _headers(cfg: Config) -> dict[str, str]:
    return {"X-API-Key": cfg.vexa_api_key, "Accept": "application/json"}


async def _http_get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            return resp.status, await _maybe_json(resp)


async def _http_patch_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> tuple[int, Any]:
    import aiohttp

    headers = {**headers, "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as session:
        async with session.patch(url, headers=headers, json=body) as resp:
            return resp.status, await _maybe_json(resp)


async def _maybe_json(resp: Any) -> Any:
    import json as _json

    text = await resp.text()
    try:
        return _json.loads(text)
    except (ValueError, _json.JSONDecodeError):
        return text
