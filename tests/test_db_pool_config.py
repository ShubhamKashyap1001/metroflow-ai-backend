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

def test_pool_settings_are_the_tightened_step7_defaults():
    assert settings.DB_POOL_SIZE == 3
    assert settings.DB_MAX_OVERFLOW == 2
    assert settings.DB_POOL_TIMEOUT == 15
    assert settings.DB_POOL_RECYCLE == 1800

def test_pool_ceiling_did_not_grow_versus_the_previous_step():
    previous_ceiling = 5 + 5  
    new_ceiling = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    assert new_ceiling <= previous_ceiling
    assert new_ceiling == 5

def test_engine_pool_is_a_bounded_queuepool_matching_settings():
    pool = engine.pool
    assert isinstance(pool, QueuePool)
    assert pool.size() == settings.DB_POOL_SIZE
    assert pool._max_overflow == settings.DB_MAX_OVERFLOW
    assert pool._timeout == settings.DB_POOL_TIMEOUT
    assert pool._recycle == settings.DB_POOL_RECYCLE
    assert pool._pre_ping is True

def test_only_one_production_engine_exists():
    assert SessionLocal.kw["bind"] is database_module.engine

    import importlib
    import sys

    assert sys.modules["app.database.database"] is database_module
    reloaded_engine_id = id(importlib.import_module("app.database.database").engine)
    assert reloaded_engine_id == id(engine)

def test_concurrent_checkouts_cannot_exceed_the_bounded_ceiling():
    ceiling = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    attempts = ceiling + 3
    results: "queue.Queue[bool]" = queue.Queue()

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

    import time

    time.sleep(1)
    assert engine.pool.checkedout() == ceiling

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
    conns = [engine.connect() for _ in range(settings.DB_POOL_SIZE)]
    assert engine.pool.checkedout() == settings.DB_POOL_SIZE
    for c in conns:
        c.close()
    assert engine.pool.checkedout() == 0

def test_existing_db_session_can_still_query_and_close_cleanly():
    db = SessionLocal()
    try:
        result = db.execute(__import__("sqlalchemy").text("SELECT 1")).scalar()
        assert result == 1
    finally:
        db.close()
    assert engine.pool.checkedout() == 0
