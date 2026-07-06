"""One-pass orchestrator for the meeting summarizer. Run on a launchd timer: `python -m summarizer`.

Flow per pass: list Vexa completed meetings -> for each not done/poisoned -> fetch transcript
-> min-duration guard -> summarize -> enabled sinks (Obsidian note, Vexa notes) -> mark_done.
mark_done is the only commit and runs last, so a crash mid-pass is harmless (next pass redoes
it; create_note's fail-if-exists is the backstop). Failures record_failure; after 5 a meeting
is poisoned and skipped until state.json is manually cleared.

DRY_RUN runs the full pipeline (including the LLM call) but writes nothing and doesn't mark
done — safe to repeat for first-run validation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

from summarizer import config as _config_mod
from summarizer.config import Config, ConfigError
from summarizer.llm import summarize  # async; litellm lazy-imported inside
from summarizer.obsidian import assemble_note, create_note, note_path
from summarizer.state import StateStore
from summarizer.types import Meeting, MeetingMeta, Utterance
from summarizer.vexa import get_transcript, list_completed_meetings, write_notes

log = logging.getLogger("vexa-summarizer")


@dataclass
class PassResult:
    summarized: int = 0
    skipped: int = 0
    failed: int = 0
    idle: int = 0


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _meta_from(meeting: Meeting, utts: list[Utterance], duration: float) -> MeetingMeta:
    speakers = sorted({u.speaker for u in utts})
    return MeetingMeta(
        participants=speakers,
        date=meeting.start.strftime("%Y-%m-%d"),
        duration=_fmt_duration(duration),
        platform=meeting.platform,
        meeting_id=meeting.id,
        native_meeting_id=meeting.native_meeting_id,
    )


async def run_once(cfg: Config) -> PassResult:
    if not cfg.summarize_enabled:
        log.info("SUMMARIZE_ENABLED=false; skipping pass")
        return PassResult()

    store = StateStore(cfg.state_dir / "state.json")
    result = PassResult()

    try:
        meetings = await list_completed_meetings(cfg, cfg.summarize_platforms)
    except Exception as exc:  # Vexa unreachable / 5xx — leave everything un-marked; retry next tick.
        log.warning("listing meetings failed: %s", exc)
        return result

    for meeting in meetings:
        key = meeting.id
        if store.is_done(key) or store.is_poisoned(key):
            result.idle += 1
            continue
        try:
            utts = await get_transcript(cfg, meeting)
            duration = sum(u.end_time - u.start_time for u in utts)
            if duration < cfg.min_transcript_seconds:
                store.mark_skipped(key, "low-transcript")
                result.skipped += 1
                log.info("meeting %s skipped: %.1fs < %.1fs min", key, duration, cfg.min_transcript_seconds)
                continue

            meta = _meta_from(meeting, utts, duration)
            summary_md = await summarize(utts, meta, cfg)
            note_md = assemble_note(meta, summary_md, utts, cfg)
            path = note_path(meeting, meta.participants, cfg) if cfg.obsidian_enabled else None

            if cfg.dry_run:
                result.summarized += 1
                log.info("[DRY_RUN] meeting %s -> would write %s", key, path)
                continue

            if cfg.obsidian_enabled:
                await create_note(cfg, path, note_md)  # type: ignore[arg-type]
            if cfg.vexa_notes_enabled:
                await write_notes(cfg, meeting, note_md)
            store.mark_done(key, path)
            result.summarized += 1
            log.info("meeting %s summarized -> %s", key, path)
        except Exception as exc:
            store.record_failure(key)
            result.failed += 1
            log.warning("meeting %s failed: %s", key, exc)

    return result


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    try:
        cfg = _config_mod.load_config()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    result = asyncio.run(run_once(cfg))
    log.info(
        "pass complete: %d summarized, %d skipped, %d failed, %d idle",
        result.summarized,
        result.skipped,
        result.failed,
        result.idle,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
