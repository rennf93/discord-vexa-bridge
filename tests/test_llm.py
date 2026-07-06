"""Tests for summarizer.llm — build_prompt (pure), parse_response (pure), summarize (seam)."""

import pytest

from summarizer import config, llm
from summarizer.types import MeetingMeta, Utterance


def _cfg(model="anthropic/claude-sonnet-5", include_transcript=True, base_url=None, key="sk-test"):
    return config.Config(ai_model=model, ai_base_url=base_url, ai_api_key=key, include_transcript=include_transcript)


def _meta():
    return MeetingMeta(
        participants=["David", "Renzo"],
        date="2026-07-06",
        duration="00:13:20",
        platform="discord",
        meeting_id=7,
        native_meeting_id="d7",
    )


def _transcript():
    return [
        Utterance("David", 0.0, 5.2, "Let's ship the summarizer."),
        Utterance("Renzo", 5.2, 12.0, "Agreed. I'll wire the launchd poller."),
    ]


# A valid LLM response satisfying all 7 required headings (used by the summarize seam tests).
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
        "- [ ] t — @o",
        "## Open questions",
        "- q",
        "## Full breakdown",
        "stuff",
        "",
    ]
)


def test_build_prompt_has_system_and_user_roles():
    msgs = llm.build_prompt(_transcript(), _meta(), _cfg())
    assert isinstance(msgs, list) and len(msgs) == 2
    roles = [m["role"] for m in msgs]
    assert roles == ["system", "user"]
    assert isinstance(msgs[0]["content"], str) and msgs[1]["content"]


def test_system_message_lists_section_contract():
    msgs = llm.build_prompt(_transcript(), _meta(), _cfg())
    sysc = msgs[0]["content"]
    for heading in [
        "TL;DR",
        "Key points",
        "Talking points",
        "Decisions",
        "Action items",
        "Open questions",
        "Full breakdown",
    ]:
        assert heading in sysc, heading
    # action items must carry an owner
    assert "@owner" in sysc.lower() or "@owner" in sysc


def test_user_message_includes_speakers_transcript_and_meta():
    msgs = llm.build_prompt(_transcript(), _meta(), _cfg())
    uc = msgs[1]["content"]
    assert "David" in uc and "Renzo" in uc
    assert "Let's ship the summarizer." in uc
    assert "2026-07-06" in uc
    assert "00:13:20" in uc


def test_parse_response_valid_returns_markdown():
    raw = (
        "Sure, here's the summary:\n\n"
        "## TL;DR\nDid the thing.\n\n"
        "## Key points\n- a\n\n"
        "## Talking points\n- b\n\n"
        "## Decisions\n- c\n\n"
        "## Action items\n- [ ] do it — @Renzo\n\n"
        "## Open questions\n- d\n\n"
        "## Full breakdown\n### Topic\nstuff\n"
    )
    md = llm.parse_response(raw)
    assert md.startswith("## TL;DR")
    assert "Sure" not in md  # wrapper prose stripped


def test_parse_response_missing_heading_raises():
    raw = "## TL;DR\nx\n\n## Key points\n- a\n"  # missing 5 headings
    with pytest.raises(ValueError, match="missing"):
        llm.parse_response(raw)


def test_parse_response_transcript_heading_optional():
    raw = (
        "## TL;DR\nx\n## Key points\n- a\n## Talking points\n- b\n"
        "## Decisions\n- c\n## Action items\n- [ ] t — @o\n"
        "## Open questions\n- q\n## Full breakdown\nstuff\n"
        "## Transcript\nraw here\n"
    )
    md = llm.parse_response(raw)
    assert "## Transcript" in md  # kept when present


class _FakeResp:
    def __init__(self, text):
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]


async def test_summarize_uses_call_api_seam_and_returns_parsed(monkeypatch):
    calls = {}

    def fake_call_api(model, messages, api_base=None, api_key=None, **kw):
        calls["model"] = model
        calls["messages"] = messages
        calls["api_base"] = api_base
        calls["api_key"] = api_key
        return _FakeResp(_GOOD_MD)

    out = await llm.summarize(
        _transcript(),
        _meta(),
        _cfg(model="ollama/llama3", base_url="http://localhost:11434", key=None),
        call_api=fake_call_api,
    )
    assert out.startswith("## TL;DR")
    assert calls["model"] == "ollama/llama3"
    assert calls["api_base"] == "http://localhost:11434"
    assert calls["api_key"] is None
    assert calls["messages"] == llm.build_prompt(_transcript(), _meta(), _cfg())


async def test_summarize_lazy_imports_litellm_when_no_seam(monkeypatch):
    """Default path imports litellm lazily; we inject a fake litellm module to avoid the dep."""
    import sys
    import types as _types

    fake = _types.ModuleType("litellm")

    def fake_completion(model, messages, api_base=None, api_key=None, **kw):
        return _FakeResp(_GOOD_MD)

    fake.completion = fake_completion
    monkeypatch.setitem(sys.modules, "litellm", fake)
    out = await llm.summarize(_transcript(), _meta(), _cfg())
    assert out.startswith("## TL;DR")
