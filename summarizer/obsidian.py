"""Obsidian sink — writes a structured note via the local vault-as-mcp plugin.

note_path is pure (derives the note location from meeting start + sorted participants).
create_note is a JSON-RPC `tools/call` over Streamable HTTP POST to OBSIDIAN_MCP_URL with a
bearer token. A "file already exists" response is treated as success — that's the
crash-recovery backstop: if we crashed between create_note and state.mark_done, the next pass
re-attempts, gets "exists", and marks done. The MCP stays 127.0.0.1-only on the Mac.

HTTP is a single async seam (_http_post) so tests fake it without aiohttp.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summarizer.config import Config
    from summarizer.types import Meeting, MeetingMeta, Utterance


class ObsidianError(RuntimeError):
    """Raised when create_note fails for a non-"exists" reason."""


def frontmatter(meta: MeetingMeta) -> str:
    parts = [
        "---",
        f"meeting_id: {meta.meeting_id}",
        f"platform: {meta.platform}",
        f"date: {meta.date}",
        "participants:",
        *[f"  - {p}" for p in meta.participants],
        f"duration: {meta.duration}",
        f"vexa_meeting_id: {meta.native_meeting_id}",
        "---",
        "",
    ]
    return "\n".join(parts) + "\n"


def assemble_note(meta: MeetingMeta, summary_md: str, transcript: list[Utterance], cfg: Config) -> str:
    """YAML frontmatter + the LLM's structured summary + optional raw transcript."""
    note = frontmatter(meta) + summary_md.rstrip() + "\n"
    if cfg.include_transcript:
        note += "\n## Transcript\n\n" + _render_transcript(transcript) + "\n"
    return note


def _render_transcript(transcript: list[Utterance]) -> str:
    lines = []
    for u in transcript:
        s = int(u.start_time)
        ts = f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
        lines.append(f"[{ts}] {u.speaker}: {u.text}")
    return "\n".join(lines)


def note_path(meeting: Meeting, speakers: list[str], cfg: Config) -> str:
    """{folder}/{YYYY-MM-DD} — {sorted participants joined by ' & '} ({HH-MM}).md

    The time uses HH-MM, not HH:MM — Obsidian filenames cannot contain ':'
    (vault-as-mcp rejects '\\ / :' in the filename portion of `path`).
    """
    date = meeting.start.strftime("%Y-%m-%d")
    time = meeting.start.strftime("%H-%M")
    participants = " & ".join(sorted(set(speakers)))
    return f"{cfg.obsidian_note_folder}/{date} — {participants} ({time}).md"


async def create_note(cfg: Config, path: str, content: str) -> None:
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_note", "arguments": {"path": path, "content": content}},
        }
    ).encode()
    headers = {
        "Authorization": f"Bearer {cfg.obsidian_mcp_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/json",
    }
    status, text = await _http_post(cfg.obsidian_mcp_url, headers, body)
    if status != 200:
        raise ObsidianError(f"Obsidian MCP returned HTTP {status}: {text[:200]}")
    payload: dict[str, Any] = _safe_json(text)
    msg = _payload_error_text(payload)
    if msg is not None:
        if "exist" in msg.lower():
            return  # idempotent backstop — note is already there
        raise ObsidianError(msg)
    return


def _payload_error_text(payload: dict[str, Any]) -> str | None:
    """Pull an error message out of a JSON-RPC response, or None on success.

    Two failure shapes to honor:
    - top-level {"error": {"message": ...}} (JSON-RPC level)
    - {"result": {"isError": true, "content": [{"type":"text","text":...}]}} (tool-execution
      failure returned with HTTP 200 — e.g. "File name cannot contain ... \\/:" or
      "File already exists"). Without this, tool failures are silently swallowed.
    """
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message", err))
    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError"):
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                return str(item.get("text", ""))
        return str(result)
    return None


def _safe_json(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        raise ObsidianError(f"non-JSON response from Obsidian MCP: {text[:200]}") from None
    return loaded if isinstance(loaded, dict) else {}


async def _http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, str]:
    """Default impl: aiohttp. Tests monkeypatch this seam."""
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=body) as resp:
            return resp.status, await resp.text()
