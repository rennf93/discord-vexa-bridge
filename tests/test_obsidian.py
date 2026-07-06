"""Tests for summarizer.obsidian — note_path scheme/uniqueness (pure) + create_note JSON-RPC."""

from datetime import UTC, datetime

import pytest

from summarizer import config, obsidian
from summarizer.types import Meeting, MeetingMeta, Utterance


def _cfg(folder="Meetings", token="tok", url="http://localhost:8765/mcp"):
    return config.Config(
        obsidian_enabled=True, obsidian_mcp_url=url, obsidian_mcp_token=token, obsidian_note_folder=folder
    )


def _meeting(start: datetime, mid=7, platform="discord", native="abc"):
    return Meeting(id=mid, platform=platform, native_meeting_id=native, start=start, end=start)


def test_note_path_scheme():
    start = datetime(2026, 7, 6, 13, 20, tzinfo=UTC)
    m = _meeting(start)
    path = obsidian.note_path(m, ["Renzo", "David"], _cfg())
    # HH-MM, not HH:MM — Obsidian filenames cannot contain ':'
    assert path == "Meetings/2026-07-06 — David & Renzo (13-20).md"
    assert ":" not in path


def test_note_path_participants_sorted_unique_joined_with_amp():
    start = datetime(2026, 7, 6, 9, 5, tzinfo=UTC)
    m = _meeting(start)
    path = obsidian.note_path(m, ["David", "Renzo", "David"], _cfg())
    # duplicates removed, sorted, joined with " & "
    assert "David & Renzo" in path
    assert path.count("David") == 1
    assert path.count("Renzo") == 1


def test_note_path_uniqueness_across_same_day_same_people_via_time():
    start1 = datetime(2026, 7, 6, 13, 20, tzinfo=UTC)
    start2 = datetime(2026, 7, 6, 15, 0, tzinfo=UTC)
    p1 = obsidian.note_path(_meeting(start1), ["David", "Renzo"], _cfg())
    p2 = obsidian.note_path(_meeting(start2), ["David", "Renzo"], _cfg())
    assert p1 != p2


def test_note_path_respects_folder_config():
    start = datetime(2026, 7, 6, 13, 20, tzinfo=UTC)
    path = obsidian.note_path(_meeting(start), ["Renzo"], _cfg(folder="Calls/Work"))
    assert path.startswith("Calls/Work/")


async def test_create_note_sends_correct_jsonrpc_and_bearer(monkeypatch):
    captured = {}

    async def fake_post(url, headers, body):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = body
        return 200, '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}'

    monkeypatch.setattr(obsidian, "_http_post", fake_post)
    await obsidian.create_note(_cfg(), "Meetings/x.md", "# content")
    assert captured["url"] == "http://localhost:8765/mcp"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    import json as _json

    payload = _json.loads(captured["body"])
    assert payload["method"] == "tools/call"
    assert payload["params"]["name"] == "create_note"
    assert payload["params"]["arguments"]["path"] == "Meetings/x.md"
    assert payload["params"]["arguments"]["content"] == "# content"


async def test_create_note_file_exists_is_success(monkeypatch):
    async def fake_post(url, headers, body):
        return 200, '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"file already exists"}}'

    monkeypatch.setattr(obsidian, "_http_post", fake_post)
    # must not raise — exists is the idempotent backstop
    await obsidian.create_note(_cfg(), "Meetings/x.md", "# content")


async def test_create_note_other_error_raises(monkeypatch):
    async def fake_post(url, headers, body):
        return 200, '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"permission denied"}}'

    monkeypatch.setattr(obsidian, "_http_post", fake_post)
    with pytest.raises(obsidian.ObsidianError, match="permission denied"):
        await obsidian.create_note(_cfg(), "Meetings/x.md", "# content")


async def test_create_note_tool_execution_failure_iserror_raises(monkeypatch):
    """HTTP 200 + result.isError=true (tool-execution failure) must raise, not be swallowed."""

    async def fake_post(url, headers, body):
        # Real shape returned by vault-as-mcp for an illegal filename char.
        return (
            200,
            '{"jsonrpc":"2.0","id":1,"result":{"content":['
            '{"type":"text","text":"Tool execution failed: File name cannot contain '
            'any of the following characters: \\\\ / :"}],"isError":true}}',
        )

    monkeypatch.setattr(obsidian, "_http_post", fake_post)
    with pytest.raises(obsidian.ObsidianError, match="File name cannot contain"):
        await obsidian.create_note(_cfg(), "Meetings/x.md", "# content")


async def test_create_note_iserror_file_exists_is_success(monkeypatch):
    """'File already exists' surfaces as result.isError — still the idempotent backstop."""

    async def fake_post(url, headers, body):
        return (
            200,
            '{"jsonrpc":"2.0","id":1,"result":{"content":['
            '{"type":"text","text":"Tool execution failed: File already exists: Meetings/x.md"}'
            '],"isError":true}}',
        )

    monkeypatch.setattr(obsidian, "_http_post", fake_post)
    # must not raise — exists is the crash-recovery backstop
    await obsidian.create_note(_cfg(), "Meetings/x.md", "# content")


async def test_create_note_http_500_raises(monkeypatch):
    async def fake_post(url, headers, body):
        return 500, "internal error"

    monkeypatch.setattr(obsidian, "_http_post", fake_post)
    with pytest.raises(obsidian.ObsidianError, match="500"):
        await obsidian.create_note(_cfg(), "Meetings/x.md", "# content")


# --- note assembly ---------------------------------------------------------


def _meta():
    return MeetingMeta(
        participants=["David", "Renzo"],
        date="2026-07-06",
        duration="00:13:20",
        platform="discord",
        meeting_id=7,
        native_meeting_id="d7",
    )


def _summary_md():
    return "## TL;DR\nDid the thing.\n\n## Key points\n- a\n\n## Full breakdown\nstuff\n"


def _transcript():
    return [Utterance("David", 0.0, 5.0, "Hello."), Utterance("Renzo", 5.0, 12.0, "Hi there.")]


def test_assemble_note_frontmatter_then_summary_then_transcript():
    cfg = _cfg()
    note = obsidian.assemble_note(_meta(), _summary_md(), _transcript(), cfg)
    assert note.startswith("---\n")
    assert "meeting_id: 7" in note
    assert "platform: discord" in note
    assert "  - David\n  - Renzo" in note
    assert "duration: 00:13:20" in note
    assert "vexa_meeting_id: d7" in note
    # summary section present
    assert "## TL;DR" in note
    # transcript appended
    assert "## Transcript" in note
    assert "[00:00:00] David: Hello." in note
    assert "[00:00:05] Renzo: Hi there." in note


def test_assemble_note_omits_transcript_when_disabled():
    cfg = _cfg()
    cfg.include_transcript = False
    note = obsidian.assemble_note(_meta(), _summary_md(), _transcript(), cfg)
    assert "## Transcript" not in note
    assert "## TL;DR" in note
