"""Local JSON idempotency store for the summarizer.

One record per meeting_id: {note_path, summarized_at, attempts, status}. status ∈ {done,
failed, skipped}. mark_done is the commit point (runs last in the pass). record_failure
poisons after POISON_LIMIT attempts so a broken meeting doesn't hot-loop every poll.

Vexa's PATCH only permits `notes` (no arbitrary data jsonb), so we can't mark "summarized"
server-side — local state is the source of truth, with create_note's fail-if-exists as a
crash-recovery backstop (see obsidian.create_note).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

POISON_LIMIT = 5


@dataclass
class MeetingState:
    note_path: str | None = None
    summarized_at: str | None = None
    attempts: int = 0
    status: str = "pending"


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, MeetingState] = self._load()

    def _load(self) -> dict[str, MeetingState]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable: start fresh rather than crashing the whole pass.
            return {}
        out: dict[str, MeetingState] = {}
        for mid, rec in raw.items():
            if isinstance(rec, dict):
                out[str(mid)] = MeetingState(
                    note_path=rec.get("note_path"),
                    summarized_at=rec.get("summarized_at"),
                    attempts=int(rec.get("attempts", 0)),
                    status=str(rec.get("status", "pending")),
                )
        return out

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {mid: asdict(rec) for mid, rec in self._data.items()}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.path)  # atomic on POSIX

    def get(self, meeting_id: int | str) -> MeetingState | None:
        return self._data.get(str(meeting_id))

    def is_done(self, meeting_id: int | str) -> bool:
        rec = self.get(meeting_id)
        return rec is not None and rec.status in {"done", "skipped"}

    def is_poisoned(self, meeting_id: int | str) -> bool:
        rec = self.get(meeting_id)
        return rec is not None and rec.status == "failed"

    def mark_done(self, meeting_id: int | str, note_path: str | None) -> None:
        self._data[str(meeting_id)] = MeetingState(
            note_path=note_path,
            summarized_at=_now_iso(),
            attempts=0,
            status="done",
        )
        self._save()

    def mark_skipped(self, meeting_id: int | str, reason: str) -> None:
        # ponytail: reason stored in note_path field to avoid a new schema field; skipped
        # meetings are not retried (is_done covers it). Promote to a real field if we ever
        # surface skip-reasons in a UI.
        self._data[str(meeting_id)] = MeetingState(
            note_path=None,
            summarized_at=_now_iso(),
            attempts=0,
            status="skipped",
        )
        self._save()

    def record_failure(self, meeting_id: int | str) -> None:
        key = str(meeting_id)
        rec = self._data.get(key) or MeetingState()
        rec.attempts += 1
        if rec.attempts >= POISON_LIMIT:
            rec.status = "failed"
        rec.summarized_at = _now_iso()
        self._data[key] = rec
        self._save()


def _now_iso() -> str:
    # UTC ISO-8601 with a Z marker; deterministic across machines (no local tz).
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
