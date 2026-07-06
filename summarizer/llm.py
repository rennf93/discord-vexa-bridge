"""Provider-agnostic summarization via LiteLLM.

build_prompt and parse_response are pure (no network, no deps) — fully testable. summarize is
the single impure seam: it calls `litellm.completion` (lazy-imported so the adapter image and
the test suite don't need litellm installed) or an injected `call_api` for tests.

Model string is `provider/model` (Anthropic, OpenAI, Ollama, vLLM, Groq, OpenRouter, ...),
mirroring Vexa's dashboard AI_MODEL convention so a future path-A promotion needs no config
translation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from summarizer.config import Config
    from summarizer.types import MeetingMeta, Utterance


SYSTEM_PROMPT = """\
You summarize meetings into a structured markdown note. Output ONLY the markdown, starting \
with `## TL;DR` — no preamble, no code fences. Use exactly these section headings, in order:

## TL;DR
2-3 sentences.

## Key points
- bullets

## Talking points
- bullets

## Decisions
- bullets

## Action items
- [ ] task — @owner   (checkboxes; assign each to a participant)

## Open questions
- bullets

## Full breakdown
By topic or speaker, with timestamps (`[HH:MM:SS]`).

Do not add a `## Transcript` section. Write in the meeting's language. Be concrete and specific \
to what was actually said — no filler.
"""

REQUIRED_HEADINGS = [
    "## TL;DR",
    "## Key points",
    "## Talking points",
    "## Decisions",
    "## Action items",
    "## Open questions",
    "## Full breakdown",
]


def _format_transcript(transcript: list[Utterance]) -> str:
    lines = []
    for u in transcript:
        ts = _fmt_ts(u.start_time)
        lines.append(f"[{ts}] {u.speaker}: {u.text}")
    return "\n".join(lines)


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def build_prompt(transcript: list[Utterance], meta: MeetingMeta, cfg: Config) -> list[dict[str, str]]:
    user = (
        f"Meeting: {meta.platform} call on {meta.date}, duration {meta.duration}.\n"
        f"Participants: {', '.join(meta.participants)}\n\n"
        f"Diarized transcript (timestamps are offsets from meeting start):\n\n"
        f"{_format_transcript(transcript)}\n\n"
        f"Produce the structured note now."
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def parse_response(raw: str) -> str:
    """Strip wrapper prose, validate required headings, return the markdown body.

    Anything before the first `## TL;DR` is treated as model preamble and dropped. The
    optional `## Transcript` section is preserved if present.
    """
    idx = raw.find("## TL;DR")
    body = raw[idx:] if idx != -1 else raw
    body = body.strip() + "\n"
    missing = [h for h in REQUIRED_HEADINGS if h not in body]
    if missing:
        raise ValueError(f"LLM response missing headings: {missing}")
    return body


def _extract_text(resp: Any) -> str:
    if isinstance(resp, str):
        return resp
    # litellm ModelResponse shape: resp.choices[0].message.content
    try:
        return str(resp.choices[0].message.content)
    except (AttributeError, IndexError, TypeError) as exc:
        raise ValueError(f"unrecognized LLM response shape: {type(resp).__name__}") from exc


async def summarize(
    transcript: list[Utterance],
    meta: MeetingMeta,
    cfg: Config,
    call_api: Callable[..., Any] | None = None,
) -> str:
    """Summarize via LiteLLM (or an injected seam). Returns validated markdown."""
    if call_api is None:
        import litellm  # lazy: keeps litellm out of the adapter image + test suite

        call_api = litellm.completion
    messages = build_prompt(transcript, meta, cfg)
    resp = call_api(
        model=cfg.ai_model,
        messages=messages,
        api_base=cfg.ai_base_url,
        api_key=cfg.ai_api_key,
    )
    return parse_response(_extract_text(resp))
