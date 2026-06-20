"""Tests for lazy DB pool init in bot.py.

Root cause guarded: relying on on_ready alone left `pool` None when the Discord
gateway became ready before Postgres accepted connections, so /join failed with
'NoneType' object has no attribute 'acquire'. ensure_pool() creates it on demand.
"""
import os

os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@h:5432/db")
os.environ.setdefault("VEXA_USER_ID", "1")

import asyncio  # noqa: E402

import bot as botmod  # noqa: E402  (importable now that bot.run is __main__-guarded)


class FakePool:
    pass


async def test_ensure_pool_creates_once_and_is_idempotent(monkeypatch):
    botmod.pool = None
    botmod._pool_lock = asyncio.Lock()  # bind to this test's loop
    calls = []

    async def fake_create_pool(url, **kw):
        calls.append(url)
        return FakePool()

    monkeypatch.setattr(botmod.asyncpg, "create_pool", fake_create_pool)

    p1 = await botmod.ensure_pool()
    p2 = await botmod.ensure_pool()
    assert p1 is p2
    assert botmod.pool is p1
    assert len(calls) == 1  # second call reuses, doesn't re-create


async def test_ensure_pool_concurrent_callers_create_one(monkeypatch):
    botmod.pool = None
    botmod._pool_lock = asyncio.Lock()
    calls = []

    async def slow_create_pool(url, **kw):
        await asyncio.sleep(0.01)  # widen the race window
        calls.append(url)
        return FakePool()

    monkeypatch.setattr(botmod.asyncpg, "create_pool", slow_create_pool)

    results = await asyncio.gather(*[botmod.ensure_pool() for _ in range(5)])
    assert len({id(r) for r in results}) == 1  # all share one pool
    assert len(calls) == 1  # lock prevented duplicate creation


async def test_ensure_pool_propagates_db_errors(monkeypatch):
    botmod.pool = None
    botmod._pool_lock = asyncio.Lock()

    async def failing_create_pool(url, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(botmod.asyncpg, "create_pool", failing_create_pool)

    # A real DB problem surfaces a clear error here, not a later NoneType.
    try:
        await botmod.ensure_pool()
        assert False, "expected OSError"
    except OSError as e:
        assert "connection refused" in str(e)
    assert botmod.pool is None  # not left half-initialized
