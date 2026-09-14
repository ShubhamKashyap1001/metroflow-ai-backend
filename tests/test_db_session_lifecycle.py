"""DB connection/session lifecycle verification (Render Free leak audit).

This file does not change any production code - the audit found every
session/connection lifecycle path already safe (get_db()'s try/except/
finally, every manual SessionLocal() already try/finally-closed, every
background-job run_forever() loop already closing its session every
tick - with rollback on exception - and every engine.connect() already
context-managed). These tests exist to make that already-correct
behaviour a regression-tested guarantee instead of an implicit one, per
the audit's TASKS list:

1. FastAPI DB dependency closes sessions.
2/3. Background jobs release sessions after success / after exceptions.
4/5. Simulator / train tracker release their session every tick.
6. Retention jobs (crowd + notification-bin) close sessions.
7. Notification workers (alert dispatch) release sessions even on
   exception.
8. No global/session cache is introduced.
9. (Existing tests are left untouched by this file.)
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker

import app.database.session as session_module
import app.simulator.csv_replay_simulator as csv_replay_simulator
import app.simulator.live_simulator as live_simulator
import app.simulator.notification_bin_retention as notification_bin_retention
import app.simulator.retention as retention
import app.simulator.train_simulator as train_simulator
from app.database.base import Base
from app.database.session import SessionLocal, get_db
from app.enums.alert_type import AlertType
from app.models.alert import Alert
from app.models.notification_dispatch_job import NotificationDispatchJob
from app.models.notification_log import NotificationLog
from app.models.station import Station
from app.models.user_profile import UserProfile
from app.services import alert_service
from app.services import notification_dispatch_queue as queue_mod


# =====================================================================
# 1. FastAPI DB dependency (get_db) always closes its session
# =====================================================================


def test_get_db_closes_session_on_normal_completion(monkeypatch):
    """The common path: the route runs to completion with a clean
    session (nothing dirty/new/deleted) - close() must still run, and
    rollback() must NOT be called (nothing to roll back)."""
    fake_db = MagicMock()
    fake_db.dirty = set()
    fake_db.new = set()
    fake_db.deleted = set()
    monkeypatch.setattr(session_module, "SessionLocal", lambda: fake_db)

    gen = get_db()
    db = next(gen)
    assert db is fake_db

    # FastAPI drives the generator to completion after the response is
    # built - this is what that looks like.
    with pytest.raises(StopIteration):
        next(gen)

    fake_db.close.assert_called_once()
    fake_db.rollback.assert_not_called()


def test_get_db_rolls_back_a_dirty_session_before_closing(monkeypatch):
    """A route that left pending changes on the session without an
    explicit commit/exception must still have them rolled back before
    the connection goes back to the pool."""
    fake_db = MagicMock()
    fake_db.dirty = {object()}
    fake_db.new = set()
    fake_db.deleted = set()
    monkeypatch.setattr(session_module, "SessionLocal", lambda: fake_db)

    gen = get_db()
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)

    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()


def test_get_db_closes_session_when_the_route_raises(monkeypatch):
    """An unhandled exception deep in a route/service must still reach
    rollback() then close() - never leaving a half-written session
    handed back to the pool."""
    fake_db = MagicMock()
    monkeypatch.setattr(session_module, "SessionLocal", lambda: fake_db)

    gen = get_db()
    next(gen)
    with pytest.raises(RuntimeError):
        gen.throw(RuntimeError("route blew up"))

    fake_db.rollback.assert_called_once()
    fake_db.close.assert_called_once()


# =====================================================================
# 8. No global/session cache is introduced
# =====================================================================


def test_session_local_is_a_factory_not_a_cached_instance():
    """SessionLocal must keep producing a brand-new Session object on
    every call - never the same instance handed out twice (which would
    amount to a hidden global session)."""
    assert isinstance(SessionLocal, sessionmaker)
    db1 = SessionLocal()
    try:
        db2 = SessionLocal()
        try:
            assert db1 is not db2
        finally:
            db2.close()
    finally:
        db1.close()


# =====================================================================
# Background loops: simulator / train tracker / retention jobs.
#
# Shared helper: a fake session_factory that hands out a fresh
# MagicMock "session" per call (recorded for inspection), and a way to
# run run_forever() for exactly N iterations by making the loop's own
# asyncio.sleep(...) raise CancelledError on the Nth call.
# =====================================================================


def _fake_session_factory():
    created = []

    def factory():
        db = MagicMock(name=f"fake_session_{len(created)}")
        created.append(db)
        return db

    return factory, created


def _run_n_iterations(coro_factory, n):
    """Drives an async run_forever(...) coroutine for exactly n loop
    iterations by making its asyncio.sleep(...) call raise
    CancelledError on the nth call - the same signal a real task
    cancellation would deliver between ticks."""
    sleep_effects = [None] * (n - 1) + [asyncio.CancelledError()]
    with patch("asyncio.sleep", new=AsyncMock(side_effect=sleep_effects)):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(coro_factory())


# --- 4. Crowd simulator (live) releases its session every tick --------


def test_live_simulator_closes_session_every_tick_on_success():
    factory, created = _fake_session_factory()
    with patch.object(live_simulator, "simulate_tick", new=AsyncMock(return_value=[])):
        _run_n_iterations(lambda: live_simulator.run_forever(factory, interval_seconds=1), 3)

    assert len(created) == 3
    for db in created:
        db.close.assert_called_once()


def test_live_simulator_closes_session_even_when_tick_raises():
    factory, created = _fake_session_factory()
    tick = AsyncMock(side_effect=[RuntimeError("tick failed"), []])
    with patch.object(live_simulator, "simulate_tick", new=tick):
        _run_n_iterations(lambda: live_simulator.run_forever(factory, interval_seconds=1), 2)

    assert len(created) == 2
    created[0].close.assert_called_once()
    created[1].close.assert_called_once()


# --- CSV-replay crowd simulator: same guarantees -----------------------


def test_csv_replay_simulator_closes_session_every_tick_on_success():
    factory, created = _fake_session_factory()
    with patch.object(csv_replay_simulator, "replay_tick", new=AsyncMock(return_value=[])):
        _run_n_iterations(
            lambda: csv_replay_simulator.run_forever(factory, interval_seconds=1), 3
        )

    assert len(created) == 3
    for db in created:
        db.close.assert_called_once()
        db.rollback.assert_not_called()


def test_csv_replay_simulator_rolls_back_and_closes_on_exception():
    factory, created = _fake_session_factory()
    tick = AsyncMock(side_effect=[RuntimeError("partial write"), []])
    with patch.object(csv_replay_simulator, "replay_tick", new=tick):
        _run_n_iterations(
            lambda: csv_replay_simulator.run_forever(factory, interval_seconds=1), 2
        )

    assert len(created) == 2
    created[0].rollback.assert_called_once()
    created[0].close.assert_called_once()
    created[1].close.assert_called_once()


# --- 5. Train tracker releases its session every tick ------------------


def test_train_tracker_closes_session_every_tick_on_success():
    factory, created = _fake_session_factory()
    with patch.object(train_simulator, "track_tick", new=AsyncMock(return_value=[])):
        _run_n_iterations(
            lambda: train_simulator.run_forever(factory, interval_seconds=1), 3
        )

    assert len(created) == 3
    for db in created:
        db.close.assert_called_once()


def test_train_tracker_rolls_back_and_closes_on_exception():
    factory, created = _fake_session_factory()
    tick = AsyncMock(side_effect=[RuntimeError("boom"), []])
    with patch.object(train_simulator, "track_tick", new=tick):
        _run_n_iterations(
            lambda: train_simulator.run_forever(factory, interval_seconds=1), 2
        )

    assert len(created) == 2
    created[0].rollback.assert_called_once()
    created[0].close.assert_called_once()
    created[1].close.assert_called_once()


# --- 6. Retention jobs close sessions -----------------------------------


def test_crowd_retention_closes_session_every_pass_on_success():
    factory, created = _fake_session_factory()
    with patch.object(retention, "run_retention_once", return_value={}):
        _run_n_iterations(
            lambda: retention.run_forever(factory, interval_seconds=1), 3
        )

    assert len(created) == 3
    for db in created:
        db.close.assert_called_once()
        db.rollback.assert_not_called()


def test_crowd_retention_rolls_back_and_closes_on_exception():
    factory, created = _fake_session_factory()
    with patch.object(
        retention, "run_retention_once", side_effect=[RuntimeError("pass failed"), {}]
    ):
        _run_n_iterations(
            lambda: retention.run_forever(factory, interval_seconds=1), 2
        )

    assert len(created) == 2
    created[0].rollback.assert_called_once()
    created[0].close.assert_called_once()
    created[1].close.assert_called_once()


def test_notification_bin_retention_closes_session_every_pass_on_success():
    factory, created = _fake_session_factory()
    with patch.object(notification_bin_retention, "run_retention_once", return_value={}):
        _run_n_iterations(
            lambda: notification_bin_retention.run_forever(factory, interval_seconds=1), 3
        )

    assert len(created) == 3
    for db in created:
        db.close.assert_called_once()
        db.rollback.assert_not_called()


def test_notification_bin_retention_rolls_back_and_closes_on_exception():
    factory, created = _fake_session_factory()
    with patch.object(
        notification_bin_retention,
        "run_retention_once",
        side_effect=[RuntimeError("pass failed"), {}],
    ):
        _run_n_iterations(
            lambda: notification_bin_retention.run_forever(factory, interval_seconds=1), 2
        )

    assert len(created) == 2
    created[0].rollback.assert_called_once()
    created[0].close.assert_called_once()
    created[1].close.assert_called_once()


# =====================================================================
# 7. Notification / alert-dispatch workers release sessions even on
#    exception. Uses a real (in-memory SQLite) session factory with
#    Session.close spied-on-but-not-replaced, so behaviour is verified
#    against genuine ORM commit/rollback/close semantics rather than a
#    mock standing in for the whole session.
# =====================================================================


@pytest.fixture
def sqlite_alert_session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Station.__table__,
            UserProfile.__table__,
            Alert.__table__,
            NotificationDispatchJob.__table__,
            NotificationLog.__table__,
        ],
    )
    TestSessionLocal = sessionmaker(
        bind=engine, autocommit=False, autoflush=False, expire_on_commit=False
    )
    monkeypatch.setattr(alert_service, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(queue_mod, "SessionLocal", TestSessionLocal)

    db = TestSessionLocal()
    try:
        station = Station(
            station_code="CTR",
            station_name="Central",
            city="Metro City",
            latitude=0.0,
            longitude=0.0,
            is_interchange=False,
            is_active=True,
            capacity=100,
        )
        db.add(station)
        db.commit()
        db.refresh(station)

        # At least one active, non-simulated recipient so a dispatch
        # actually reaches step 3 (the logging session) instead of
        # returning early after step 1 with nothing to send.
        db.add(
            UserProfile(
                id=uuid.uuid4(),
                email="alice@example.com",
                full_name="Alice",
                is_active=True,
            )
        )
        db.commit()

        alert = Alert(
            station_id=station.id,
            alert_type=AlertType.DELAY,
            message="Signal fault",
            is_resolved=False,
            notify_email=True,
            notify_sms=False,
        )
        db.add(alert)
        db.commit()
        db.refresh(alert)
        alert_id = alert.id
    finally:
        db.close()

    return TestSessionLocal, alert_id


def _spy_on_session_close():
    """Counts real Session.close() calls while still performing the
    real close - a spy, not a stand-in mock."""
    return patch.object(SASession, "close", autospec=True, side_effect=SASession.close)


def test_alert_dispatch_closes_both_sessions_on_success(sqlite_alert_session_factory):
    TestSessionLocal, alert_id = sqlite_alert_session_factory

    with _spy_on_session_close() as mock_close:
        with patch.object(alert_service, "send_alert_emails", return_value={}):
            alert_service.dispatch_alert_notifications(alert_id, None, True, False)

    # Step 1's read-only session (db1) and step 3's logging session
    # (db2) - two short-lived sessions, both closed.
    assert mock_close.call_count == 2


def test_alert_dispatch_closes_step1_session_even_if_it_raises(sqlite_alert_session_factory):
    """A failure while still inside the first (read) session's try
    block must not skip that session's close() - and must never reach
    the network-call / step-3 session at all."""
    TestSessionLocal, alert_id = sqlite_alert_session_factory

    with _spy_on_session_close() as mock_close:
        with patch.object(
            alert_service, "_already_sent_recipients", side_effect=RuntimeError("query blew up")
        ):
            with pytest.raises(RuntimeError):
                alert_service.dispatch_alert_notifications(
                    alert_id, None, True, False, job_id=1
                )

    # Only db1 was ever opened, and it was still closed despite the
    # exception raised inside its try block.
    assert mock_close.call_count == 1


def test_alert_dispatch_rolls_back_and_closes_step3_session_on_logging_failure(
    sqlite_alert_session_factory,
):
    """A failure while logging results (db2, step 3 - after the slow
    network call already happened) must roll back and close db2, and
    must not leave db1 (already closed after step 1) reopened."""
    TestSessionLocal, alert_id = sqlite_alert_session_factory

    with _spy_on_session_close() as mock_close:
        with patch.object(alert_service, "send_alert_emails", return_value={}), patch.object(
            alert_service, "_log_results", side_effect=RuntimeError("logging failed")
        ):
            with pytest.raises(RuntimeError):
                alert_service.dispatch_alert_notifications(alert_id, None, True, False)

    # db1 (step 1) + db2 (step 3, where the failure happened) - both closed.
    assert mock_close.call_count == 2


def test_notification_dispatch_queue_closes_session_on_success(sqlite_alert_session_factory):
    TestSessionLocal, alert_id = sqlite_alert_session_factory

    with _spy_on_session_close() as mock_close:
        with patch.object(queue_mod.notification_executor, "submit"):
            queue_mod.enqueue_and_submit(
                queue_mod.NotificationDispatchKind.ALERT_CREATED,
                alert_id=alert_id,
                actor_id=None,
                notify_email=True,
                notify_sms=False,
            )

    assert mock_close.call_count == 1


def test_notification_dispatch_queue_closes_session_when_enqueue_raises(
    sqlite_alert_session_factory,
):
    TestSessionLocal, alert_id = sqlite_alert_session_factory

    with _spy_on_session_close() as mock_close:
        with patch.object(SASession, "commit", side_effect=RuntimeError("db down")):
            with pytest.raises(RuntimeError):
                queue_mod.enqueue_and_submit(
                    queue_mod.NotificationDispatchKind.ALERT_CREATED,
                    alert_id=alert_id,
                    actor_id=None,
                    notify_email=True,
                    notify_sms=False,
                )

    assert mock_close.call_count == 1


def test_run_job_closes_every_session_it_opens_on_failure(sqlite_alert_session_factory):
    """run_job opens one session to mark IN_PROGRESS/read fields, then
    (on failure) another via _mark() to record FAILED - both must be
    closed, and the original exception must still propagate."""
    TestSessionLocal, alert_id = sqlite_alert_session_factory

    db = TestSessionLocal()
    try:
        job = NotificationDispatchJob(
            kind=queue_mod.NotificationDispatchKind.ALERT_CREATED,
            alert_id=alert_id,
            actor_id=None,
            notify_email=True,
            notify_sms=False,
            status=queue_mod.NotificationDispatchStatus.QUEUED,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id
    finally:
        db.close()

    with _spy_on_session_close() as mock_close:
        with patch.object(
            alert_service, "dispatch_alert_notifications", side_effect=RuntimeError("smtp exploded")
        ):
            with pytest.raises(RuntimeError):
                queue_mod.run_job(job_id)

    # One session to mark IN_PROGRESS + one session inside _mark() to
    # record FAILED - both closed even though the job failed.
    assert mock_close.call_count == 2
