"""End-to-end tests for summarizer.__main__.run_once — all clients faked via monkeypatch.

run_once is the one-pass orchestrator: list completed meetings -> for each not done/poisoned
-> transcript -> min-duration guard -> summarize -> enabled sinks -> mark_done (or
record_failure on any exception). DRY_RUN skips sink writes + mark_done.
"""

from datetime import UTC, datetime

from summarizer import __main__ as m
from summarizer import config
from summarizer.types import Meeting, Utterance


def _cfg(tmp_path, obsidian=True, vexa_notes=False, dry_run=False, min_sec=30.0, enabled=True):
    return config.Config(
        summarize_enabled=enabled,
        ai_model="anthropic/claude-sonnet-5",
        vexa_api_url="http://vexa:8056",
        vexa_api_key="k",
        min_transcript_seconds=min_sec,
        obsidian_enabled=obsidian,
        obsidian_mcp_token="tok" if obsidian else None,
        vexa_notes_enabled=vexa_notes,
        dry_run=dry_run,
        state_dir=tmp_path,
    )


def _meeting(mid=7, native="d7"):
    return Meeting(
        id=mid,
        platform="discord",
        native_meeting_id=native,
        start=datetime(2026, 7, 6, 13, 20, tzinfo=UTC),
        end=datetime(2026, 7, 6, 13, 33, tzinfo=UTC),
    )


def _utts(seconds_each=20.0):
    # two utterances -> total duration 2*seconds_each
    return [
        Utterance("David", 0.0, seconds_each, "We should ship it."),
        Utterance("Renzo", seconds_each, 2 * seconds_each, "Agreed. I'll wire the poller."),
    ]


_GOOD_MD = "\n".join(
    [
        "## TL;DR",
        "x",
        "## Key points",
        "- a",
        "## Talking points",
        "- b",
        "## Decisions",
        "- c",
        "## Action items",
        "- [ ] t — @Renzo",
        "## Open questions",
        "- q",
        "## Full breakdown",
        "stuff",
        "",
    ]
)


def _patch_clients(monkeypatch, meetings, utts=None, summarize_fn=None, create_note_fn=None, write_notes_fn=None):
    async def fake_list(cfg, platforms):
        return meetings

    async def fake_transcript(cfg, meeting):
        return utts if utts is not None else _utts()

    async def fake_summarize(transcript, meta, cfg, call_api=None):
        if summarize_fn is not None:
            return summarize_fn(transcript, meta, cfg)
        return _GOOD_MD

    async def fake_create_note(cfg, path, content):
        if create_note_fn is not None:
            await create_note_fn(cfg, path, content)

    async def fake_write_notes(cfg, meeting, markdown):
        if write_notes_fn is not None:
            await write_notes_fn(cfg, meeting, markdown)

    monkeypatch.setattr(m, "list_completed_meetings", fake_list)
    monkeypatch.setattr(m, "get_transcript", fake_transcript)
    monkeypatch.setattr(m, "summarize", fake_summarize)
    monkeypatch.setattr(m, "create_note", fake_create_note)
    monkeypatch.setattr(m, "write_notes", fake_write_notes)


async def test_run_once_summarizes_writes_note_and_marks_done(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append((path, content))

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note)
    result = await m.run_once(_cfg(tmp_path))
    assert result.summarized == 1
    assert len(created) == 1
    path, content = created[0]
    assert path == "Meetings/2026-07-06 — David & Renzo (13-20).md"
    assert content.startswith("---\n")
    assert "## TL;DR" in content
    # state marked done
    from summarizer.state import StateStore

    store = StateStore(tmp_path / "state.json")
    assert store.is_done(7) is True


async def test_second_run_is_noop(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append(1)

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note)
    await m.run_once(_cfg(tmp_path))
    result = await m.run_once(_cfg(tmp_path))
    assert result.summarized == 0
    assert result.idle == 1
    assert len(created) == 1  # not re-created


async def test_low_transcript_meeting_is_skipped_not_summarized(tmp_path, monkeypatch):
    summarized = []
    _patch_clients(
        monkeypatch,
        [_meeting()],
        utts=[Utterance("David", 0.0, 5.0, "hi")],
        summarize_fn=lambda *a: summarized.append(1) or _GOOD_MD,
    )
    result = await m.run_once(_cfg(tmp_path, min_sec=30.0))
    assert result.skipped == 1
    assert result.summarized == 0
    assert summarized == []  # no LLM run
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True  # skipped = done, not retried


async def test_failure_increments_attempts_and_poisons_after_five(tmp_path, monkeypatch):
    async def boom(transcript, meta, cfg, call_api=None):
        raise RuntimeError("llm down")

    _patch_clients(monkeypatch, [_meeting()], summarize_fn=None)
    monkeypatch.setattr(m, "summarize", boom)
    for _ in range(4):
        result = await m.run_once(_cfg(tmp_path))
        assert result.failed == 1
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_poisoned(7) is False
    await m.run_once(_cfg(tmp_path))  # 5th failure poisons
    store = StateStore(tmp_path / "state.json")
    assert store.is_poisoned(7) is True


async def test_dry_run_writes_nothing_and_does_not_mark(tmp_path, monkeypatch):
    created = []

    async def create_note(cfg, path, content):
        created.append(1)

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note)
    result = await m.run_once(_cfg(tmp_path, dry_run=True))
    assert result.summarized == 1
    assert created == []  # no sink write
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is False  # not marked


async def test_disabled_obsidian_skips_create_note_but_writes_vexa_notes(tmp_path, monkeypatch):
    created = []
    written = []

    async def create_note(cfg, path, content):
        created.append(1)

    async def write_notes(cfg, meeting, markdown):
        written.append(markdown)

    _patch_clients(monkeypatch, [_meeting()], create_note_fn=create_note, write_notes_fn=write_notes)
    result = await m.run_once(_cfg(tmp_path, obsidian=False, vexa_notes=True))
    assert result.summarized == 1
    assert created == []
    assert len(written) == 1
    assert "## TL;DR" in written[0]
    from summarizer.state import StateStore

    assert StateStore(tmp_path / "state.json").is_done(7) is True


async def test_summarize_disabled_short_circuits_no_vexa_call(tmp_path, monkeypatch):
    async def must_not_be_called(*a, **k):
        raise AssertionError("list_completed_meetings should not be called when disabled")

    monkeypatch.setattr(m, "list_completed_meetings", must_not_be_called)
    result = await m.run_once(_cfg(tmp_path, enabled=False))
    assert result.summarized == 0
