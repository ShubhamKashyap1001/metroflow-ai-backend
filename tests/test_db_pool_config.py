"""Step 7 (OOM fix) — DB connection pool + session memory verification.

Render Free gives this process 512MB total. Every pooled DB connection
carries its own client-side buffers, so the pool ceiling itself is
memory pressure, not just a safety margin against exhaustion. This
file locks in the tightened defaults (pool_size=3, max_overflow=2,
pool_timeout=15, pool_recycle=1800, pool_pre_ping=True) as a
regression-tested contract, and proves the pool is a real bounded
ceiling against the actual SQLAlchemy engine/Postgres — not just
correct-looking config values that are never exercised.

Session-lifecycle correctness (get_db, background loops, manual
SessionLocal() call sites) is already covered by
tests/test_db_session_lifecycle.py and is intentionally not
duplicated here.
"""
from __future__ import annotations

import queue
import threading

import pytest
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.pool import QueuePool

import app.database.database as database_module
from app.core.config import settings
from app.database.database import engine
from app.database.session import SessionLocal


# =====================================================================
# 1. Settings carry the tightened Step 7 defaults
# =====================================================================


def test_pool_settings_are_the_tightened_step7_defaults():
    """Locks in the specific numbers this step was asked to ship, so a
    future change can't silently widen the pool back out on a 512MB
    instance without a test noticing."""
    assert settings.DB_POOL_SIZE == 3
    assert settings.DB_MAX_OVERFLOW == 2
    assert settings.DB_POOL_TIMEOUT == 15
    assert settings.DB_POOL_RECYCLE == 1800


def test_pool_ceiling_did_not_grow_versus_the_previous_step():
    """The OOM fix must never *raise* pool_size/max_overflow (see step
    instructions #7) — only tighten or hold steady."""
    previous_ceiling = 5 + 5  # the prior (already-tightened) 15/25 -> 5/5 pass
    new_ceiling = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    assert new_ceiling <= previous_ceiling
    assert new_ceiling == 5


# =====================================================================
# 2. The real engine/pool object is actually configured this way
# =====================================================================


def test_engine_pool_is_a_bounded_queuepool_matching_settings():
    """Reads the values off the live `engine.pool` object itself
    (not just settings) so a refactor that stops passing these
    kwargs to create_engine() would fail this test even if
    config.py's defaults still look right."""
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    assert pool.size() == settings.DB_POOL_SIZE
    assert pool._max_overflow == settings.DB_MAX_OVERFLOW
    assert pool._timeout == settings.DB_POOL_TIMEOUT
    assert pool._recycle == settings.DB_POOL_RECYCLE
    assert pool._pre_ping is True


def test_only_one_production_engine_exists():
    """Guards against a duplicate engine being created by an import
    cycle, a helper module, or a background service — the ORM's
    `Session` and every raw `engine.connect()` call in the app must
    all resolve to this single, singleton engine object."""
    assert SessionLocal.kw["bind"] is database_module.engine
    # Re-importing the module must return the cached module object,
    # never re-run `create_engine(...)` a second time.
    import importlib
    import sys

    assert sys.modules["app.database.database"] is database_module
    reloaded_engine_id = id(importlib.import_module("app.database.database").engine)
    assert reloaded_engine_id == id(engine)


# =====================================================================
# 3. The pool is a REAL ceiling, not just plausible-looking config
# =====================================================================


def test_concurrent_checkouts_cannot_exceed_the_bounded_ceiling():
    """Drives more concurrent raw connection checkouts than
    pool_size + max_overflow against the real engine/Postgres, holding
    every successful checkout open indefinitely so excess attempts are
    forced to wait out the real pool_timeout rather than succeeding
    late once a slot happens to free up. Every checkout beyond the
    ceiling must fail with SQLAlchemy's own TimeoutError (bounded
    wait, then a clean, catchable error) instead of silently opening
    an unbounded number of connections — and at no point may more than
    `ceiling` connections be checked out simultaneously."""
    ceiling = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    attempts = ceiling + 3
    results: "queue.Queue[bool]" = queue.Queue()
    # Held open until the test explicitly releases them - long enough
    # that it can never race with the pool_timeout wait below.
    release_event = threading.Event()

    def worker():
        try:
            conn = engine.connect()
        except SATimeoutError:
            results.put(False)
            return
        results.put(True)
        try:
            release_event.wait(timeout=settings.DB_POOL_TIMEOUT + 30)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(attempts)]
    for t in threads:
        t.start()

    # Give the first `ceiling` checkouts time to actually land, then
    # confirm the pool itself never exceeded the configured ceiling
    # while everyone is still holding/waiting.
    import time

    time.sleep(1)
    assert engine.pool.checkedout() == ceiling

    # Only now let the excess attempts finish timing out against the
    # real pool_timeout, before releasing the held connections - so a
    # freed slot can never rescue an excess attempt into a late
    # success.
    for t in threads:
        t.join(timeout=settings.DB_POOL_TIMEOUT + 10)

    release_event.set()
    for t in threads:
        t.join(timeout=10)

    outcomes = []
    while not results.empty():
        outcomes.append(results.get_nowait())

    succeeded = sum(1 for ok in outcomes if ok)
    failed = sum(1 for ok in outcomes if not ok)

    assert succeeded == ceiling, (
        f"expected exactly {ceiling} concurrent connections to succeed, got {succeeded}"
    )
    assert failed == attempts - ceiling


def test_checked_out_connections_are_released_back_to_the_pool():
    """After connections are closed, the pool must show them as
    checked back in (checkedout() == 0) — i.e. this isn't a one-shot
    ceiling that stays exhausted forever after the first burst."""
    conns = [engine.connect() for _ in range(settings.DB_POOL_SIZE)]
    assert engine.pool.checkedout() == settings.DB_POOL_SIZE
    for c in conns:
        c.close()
    assert engine.pool.checkedout() == 0


# =====================================================================
# 4. Existing DB functionality still works under the new pool config
# =====================================================================


def test_existing_db_session_can_still_query_and_close_cleanly():
    """A plain SessionLocal() round trip (the same pattern used by
    every route via get_db()) must keep working unchanged — the pool
    tightening must not have broken ordinary DB access."""
    db = SessionLocal()
    try:
        result = db.execute(__import__("sqlalchemy").text("SELECT 1")).scalar()
        assert result == 1
    finally:
        db.close()
    assert engine.pool.checkedout() == 0
