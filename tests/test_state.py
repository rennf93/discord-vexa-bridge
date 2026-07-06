"""Tests for summarizer.state — local JSON idempotency store, poison guard, atomic writes."""

from summarizer import state


def test_fresh_store_has_no_done(tmp_path):
    s = state.StateStore(tmp_path / "state.json")
    assert s.is_done(7) is False
    assert s.is_poisoned(7) is False


def test_mark_done_then_is_done(tmp_path):
    s = state.StateStore(tmp_path / "state.json")
    s.mark_done(7, "Meetings/2026-07-06 — David & Renzo (13:20).md")
    assert s.is_done(7) is True
    rec = s.get(7)
    assert rec is not None
    assert rec.note_path == "Meetings/2026-07-06 — David & Renzo (13:20).md"
    assert rec.status == "done"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "state.json"
    s = state.StateStore(path)
    s.mark_done(7, "x.md")
    s2 = state.StateStore(path)
    assert s2.is_done(7) is True


def test_record_failure_increments_and_poisons_after_five(tmp_path):
    s = state.StateStore(tmp_path / "state.json")
    for _ in range(4):
        s.record_failure(7)
    assert s.is_poisoned(7) is False
    s.record_failure(7)  # 5th
    assert s.is_poisoned(7) is True
    assert s.get(7).attempts == 5
    assert s.get(7).status == "failed"


def test_mark_skipped_is_done_not_retried(tmp_path):
    s = state.StateStore(tmp_path / "state.json")
    s.mark_skipped(7, "low-transcript")
    assert s.is_done(7) is True
    assert s.is_poisoned(7) is False
    assert s.get(7).status == "skipped"
    assert s.get(7).note_path is None


def test_corrupt_file_does_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    s = state.StateStore(path)  # must not raise
    assert s.is_done(7) is False
    s.mark_done(7, "x.md")  # writing should still work (overwrites corrupt file)
    assert s.is_done(7) is True


def test_atomic_write_leaves_no_partial(tmp_path):
    path = tmp_path / "state.json"
    s = state.StateStore(path)
    s.mark_done(7, "x.md")
    # no tmp file left behind
    assert list(tmp_path.glob("*.tmp")) == []
    assert path.exists()
