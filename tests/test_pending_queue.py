"""Tests for the durable pending queue + background drainer in bot.py.

Root cause guarded: the transcription worker is single-concurrency and CPU-slow on the
NAS, returning 503 for anything it can't accept immediately. The old `store()` discarded
the PCM on any transcribe failure, so a 43-minute call yielded 3 transcript rows. Now
every segment spills to disk and a drainer replays it; nothing is lost on 503 or restart.
"""

import os

os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@h:5432/db")
os.environ.setdefault("VEXA_USER_ID", "1")

import asyncio  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

import pytest  # noqa: E402

import bot as botmod  # noqa: E402  (importable now that bot.run is __main__-guarded)


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod, "PENDING_DIR", tmp_path)
    return tmp_path


def _spill(meeting_id=7, speaker="Renzo", t0=1.0, t1=2.0):
    botmod.spill_segment(meeting_id, "sess-uid", speaker, t0, t1, None, b"WAVBYTES")


async def test_spill_then_scan_round_trips_in_order(pending):
    _spill(t0=1.0)
    await asyncio.sleep(0.01)
    _spill(t0=2.0)
    items = botmod.scan_pending()
    assert len(items) == 2
    # oldest-first
    wav, meta = botmod.load_segment(items[0])
    assert wav == b"WAVBYTES"
    assert meta["meeting_id"] == 7
    assert meta["speaker"] == "Renzo"
    assert meta["t0"] == 1.0


async def test_store_spills_without_transcribing(pending, monkeypatch):
    """store() must never call transcribe() — it only stages to disk."""
    called = []
    monkeypatch.setattr(botmod, "transcribe", lambda *a, **k: called.append(1) or asyncio.sleep(0, result=""))
    monkeypatch.setattr(botmod, "ensure_pool", _fake_pool)

    meeting = _FakeMeeting()
    # long enough to clear MIN_MS
    pcm = b"\x00" * int(botmod.BYTES_PER_SEC * 0.5)
    await botmod.store(meeting, 42, pcm, t0=100.0, t1=100.5)

    assert called == []  # no live transcribe
    assert len(botmod.scan_pending()) == 1
    _, meta = botmod.load_segment(botmod.scan_pending()[0])
    assert meta["speaker"] == "David"
    assert meta["t0"] == 100.0 - meeting.t0


async def test_drain_once_transcribes_inserts_and_deletes(pending, monkeypatch):
    _spill(meeting_id=7, speaker="Renzo")
    inserts = []

    async def fake_transcribe(wav):
        assert wav == b"WAVBYTES"
        return "hello world"

    async def fake_insert(meta, text):
        inserts.append((meta["meeting_id"], meta["speaker"], text))

    monkeypatch.setattr(botmod, "transcribe", fake_transcribe)
    monkeypatch.setattr(botmod, "insert_transcription", fake_insert)

    status = await botmod.drain_once()
    assert status == "done"
    assert inserts == [(7, "Renzo", "hello world")]
    assert botmod.scan_pending() == []  # file removed on success


async def test_drain_once_worker_down_leaves_file_for_retry(pending, monkeypatch):
    """Worker unavailable (non-200 / connection error) → transcribe returns None → retry."""
    _spill(meeting_id=7)
    monkeypatch.setattr(botmod, "transcribe", lambda wav: asyncio.sleep(0, result=None))  # worker down
    monkeypatch.setattr(botmod, "insert_transcription", _no_insert)
    status = await botmod.drain_once()
    assert status == "failed"
    assert len(botmod.scan_pending()) == 1  # still queued


async def test_drain_once_silence_advances_and_deletes(pending, monkeypatch):
    """Worker responded 200 OK but no speech (VAD stripped it) → empty text is a legitimate
    result, NOT a failure. The clip must be deleted and the queue advanced; otherwise a single
    silence segment blocks the FIFO queue forever (root cause of the stuck-queue bug)."""
    _spill(meeting_id=7, speaker="Dollylogon")
    monkeypatch.setattr(botmod, "transcribe", lambda wav: asyncio.sleep(0, result=""))  # 200 OK, silence
    monkeypatch.setattr(botmod, "insert_transcription", _no_insert)
    status = await botmod.drain_once()
    assert status == "done"  # advanced, not retried
    assert botmod.scan_pending() == []  # clip deleted — queue not blocked by silence
    # nothing to insert, so insert_transcription must not have run


async def test_finalizing_meeting_completes_when_queue_drains(pending, monkeypatch):
    _spill(meeting_id=7)
    botmod.mark_finalizing(7)
    assert botmod.finalizing_meetings() == [7]

    stmts = []
    monkeypatch.setattr(botmod, "transcribe", lambda wav: asyncio.sleep(0, result="hi"))

    class _Conn:
        async def execute(self, sql, *args):
            stmts.append(sql)

        async def fetchrow(self, sql, *args):
            stmts.append(sql)
            return {"platform_specific_id": "999", "start_time": None, "end_time": None}

    async def fake_pool():
        return _FakeCtx(_Conn())

    monkeypatch.setattr(botmod, "ensure_pool", fake_pool)

    await botmod.drain_once()  # transcribes + inserts + finalizes
    assert botmod.scan_pending() == []
    assert any("status='completed'" in s for s in stmts)  # meeting flipped to completed
    assert botmod.finalizing_meetings() == []  # marker removed


async def test_finalize_completed_emits_webhook_when_configured(pending, monkeypatch):
    """_finalize_completed fires the webhook (as a tracked background task) once configured."""
    botmod.mark_finalizing(7)  # queue already drained, nothing spilled for meeting 7
    monkeypatch.setattr(botmod, "COMPLETION_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(botmod, "COMPLETION_WEBHOOK_SECRET", "s3cr3t")

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"platform_specific_id": "999", "start_time": start, "end_time": end}

    async def fake_pool():
        return _FakeCtx(_Conn())

    monkeypatch.setattr(botmod, "ensure_pool", fake_pool)

    emitted = []

    async def fake_emit(url, secret, envelope, **kwargs):
        emitted.append((url, secret, envelope))
        return True

    monkeypatch.setattr(botmod.completion_webhook, "emit", fake_emit)

    await botmod._finalize_completed()
    await asyncio.sleep(0)  # let the fire-and-forget task run

    assert len(emitted) == 1
    url, secret, envelope = emitted[0]
    assert url == "https://example.test/hook"
    assert secret == "s3cr3t"
    assert envelope["data"]["meeting"]["id"] == 7
    assert envelope["data"]["meeting"]["native_meeting_id"] == "999"
    assert botmod.finalizing_meetings() == []


async def test_finalize_completed_emits_webhook_with_null_end_time(pending, monkeypatch):
    """A still-open row (end_time NULL) must not raise and must not drop the webhook."""
    botmod.mark_finalizing(7)
    monkeypatch.setattr(botmod, "COMPLETION_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(botmod, "COMPLETION_WEBHOOK_SECRET", "s3cr3t")

    start = datetime(2026, 1, 1, tzinfo=UTC)

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"platform_specific_id": "999", "start_time": start, "end_time": None}

    async def fake_pool():
        return _FakeCtx(_Conn())

    monkeypatch.setattr(botmod, "ensure_pool", fake_pool)

    emitted = []

    async def fake_emit(url, secret, envelope, **kwargs):
        emitted.append(envelope)
        return True

    monkeypatch.setattr(botmod.completion_webhook, "emit", fake_emit)

    await botmod._finalize_completed()
    await asyncio.sleep(0)

    assert len(emitted) == 1
    assert "end_time" not in emitted[0]["data"]["meeting"]
    assert botmod.finalizing_meetings() == []


async def test_finalize_completed_emits_webhook_with_null_start_time(pending, monkeypatch):
    """A row with no start_time recorded must not raise and must not drop the webhook."""
    botmod.mark_finalizing(7)
    monkeypatch.setattr(botmod, "COMPLETION_WEBHOOK_URL", "https://example.test/hook")
    monkeypatch.setattr(botmod, "COMPLETION_WEBHOOK_SECRET", "s3cr3t")

    end = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"platform_specific_id": "999", "start_time": None, "end_time": end}

    async def fake_pool():
        return _FakeCtx(_Conn())

    monkeypatch.setattr(botmod, "ensure_pool", fake_pool)

    emitted = []

    async def fake_emit(url, secret, envelope, **kwargs):
        emitted.append(envelope)
        return True

    monkeypatch.setattr(botmod.completion_webhook, "emit", fake_emit)

    await botmod._finalize_completed()
    await asyncio.sleep(0)

    assert len(emitted) == 1
    assert emitted[0]["data"]["meeting"]["start_time"] == "2026-01-01T00:30:00Z"
    assert botmod.finalizing_meetings() == []


async def test_finalize_completed_skips_webhook_when_not_configured(pending, monkeypatch):
    """Default (no COMPLETION_WEBHOOK_URL): feature off, no task created, no emit call."""
    assert botmod.COMPLETION_WEBHOOK_URL is None
    botmod.mark_finalizing(7)

    class _Conn:
        async def fetchrow(self, sql, *args):
            return {"platform_specific_id": "999", "start_time": None, "end_time": None}

    async def fake_pool():
        return _FakeCtx(_Conn())

    monkeypatch.setattr(botmod, "ensure_pool", fake_pool)

    called = []
    monkeypatch.setattr(botmod.completion_webhook, "emit", lambda *a, **k: called.append(1))

    await botmod._finalize_completed()
    await asyncio.sleep(0)

    assert called == []
    assert botmod.finalizing_meetings() == []


async def test_finalizing_not_completed_while_queue_has_segments(pending, monkeypatch):
    _spill(meeting_id=7)
    botmod.mark_finalizing(7)
    monkeypatch.setattr(botmod, "transcribe", lambda wav: asyncio.sleep(0, result=None))  # worker down
    monkeypatch.setattr(botmod, "insert_transcription", _no_insert)
    monkeypatch.setattr(botmod, "ensure_pool", _fake_pool)
    await botmod.drain_once()  # failed — must NOT finalize
    assert botmod.finalizing_meetings() == [7]


async def test_drain_once_idle_when_empty(pending, monkeypatch):
    monkeypatch.setattr(botmod, "ensure_pool", _fake_pool)
    assert await botmod.drain_once() == "idle"


# --- helpers / fakes --------------------------------------------------------


class _FakeMeeting:
    id = 7
    session_uid = "sess-uid"
    t0 = 0.0

    async def name_for(self, uid):
        return "David"


class _FakeCtx:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self  # real asyncpg: pool.acquire() -> async CM yielding a connection

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


async def _fake_pool():
    return _FakeCtx(None)


async def _no_insert(meta, text):
    raise AssertionError("insert should not run in this test")
