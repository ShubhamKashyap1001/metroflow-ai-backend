"""Render Free 512MB fix: prevent excessive/unbounded application logging.

Covers the two real gaps found in the audit for this pass - everything
else already logged at the right frequency/level (verified by reading,
not by adding redundant tests for code that was already correct):

1. `app/core/log_buffer.py`'s `install()` raises the ROOT logger to
   INFO so the admin log-buffer captures useful app records. Side
   effect: any third-party logger with no level of its own (httpx,
   used internally by google-genai for every chatbot call) used to
   inherit that INFO level instead of Python's normal WARNING default,
   turning on one "HTTP Request: ..." log line per outbound call that
   was previously silent. Fixed by explicitly pinning httpx/httpcore
   back to WARNING inside install().

2. `app/services/notification_dispatch_queue.py`'s
   `recover_pending_jobs()` used to log one `logger.warning()` per
   resumed job in a loop - a per-record log in a startup recovery
   pass that could replay an unbounded number of times after a crash
   with a large backlog. Fixed by collapsing to one aggregate WARNING
   line (job IDs capped) regardless of how many jobs are resumed.
"""
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch

from app.core import log_buffer
from app.core.config import settings
from app.database.base import Base
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.notification_dispatch_status import NotificationDispatchStatus
from app.models.notification_dispatch_job import NotificationDispatchJob
from app.services import notification_dispatch_queue as queue_mod


# ---------------------------------------------------------------------
# 1. Production default logging level is not DEBUG.
# ---------------------------------------------------------------------

def test_debug_setting_defaults_to_false():
    """DEBUG-level verbose logging must not be on by default in
    production configuration (settings.DEBUG also gates other
    debug-only behavior - see app/core/config.py)."""
    from app.core.config import Settings
    assert Settings.model_fields["DEBUG"].default is False


def test_root_logger_is_info_not_debug_after_install():
    """log_buffer.install() is the one place that raises the root
    logger's level - it must land on INFO, never DEBUG."""
    root = logging.getLogger()
    previous_level = root.level
    try:
        root.setLevel(logging.WARNING)
        log_buffer.install()
        assert root.level == logging.INFO
    finally:
        root.setLevel(previous_level)


# ---------------------------------------------------------------------
# 2. Fix #1 regression: known-chatty third-party loggers stay quiet
#    even though the root logger is raised to INFO.
# ---------------------------------------------------------------------

def test_install_pins_noisy_third_party_loggers_to_warning():
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    previous = (httpx_logger.level, httpcore_logger.level)
    try:
        httpx_logger.setLevel(logging.NOTSET)
        httpcore_logger.setLevel(logging.NOTSET)

        log_buffer.install()

        assert httpx_logger.level == logging.WARNING, (
            "httpx must not inherit the root logger's INFO level - it "
            "would log one line per outbound HTTP request (every "
            "chatbot call) that used to be silent"
        )
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(previous[0])
        httpcore_logger.setLevel(previous[1])


def test_install_does_not_touch_this_app_own_logger_levels():
    """The fix must only affect third-party noise, never this app's
    own loggers (nothing under `app.*` should get an explicit level
    forced onto it by install())."""
    app_logger = logging.getLogger("app.services.chatbot_service")
    previous = app_logger.level
    try:
        app_logger.setLevel(logging.NOTSET)
        log_buffer.install()
        assert app_logger.level == logging.NOTSET
    finally:
        app_logger.setLevel(previous)


# ---------------------------------------------------------------------
# 3. Fix #2 regression + general requirement: a large processing loop
#    (here, notification-restart recovery) does not emit one log
#    record per row/job - only a bounded, aggregate line.
# ---------------------------------------------------------------------

@pytest.fixture
def sqlite_session_factory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[NotificationDispatchJob.__table__])
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(queue_mod, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def _insert_queued_job(session_factory, i):
    db = session_factory()
    try:
        job = NotificationDispatchJob(
            kind=NotificationDispatchKind.ALERT_RESOLVED,
            alert_id=i,
            actor_id=None,
            notify_email=False,
            notify_sms=False,
            status=NotificationDispatchStatus.QUEUED,
            attempts=0,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


def test_recovering_many_jobs_emits_one_aggregate_log_not_one_per_job(sqlite_session_factory, caplog):
    """Requirement: high-frequency/large loops must not emit one log
    record per processed row. 50 resumable jobs must still produce
    exactly one WARNING line from recover_pending_jobs(), not 50."""
    job_ids = [_insert_queued_job(sqlite_session_factory, i) for i in range(50)]

    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        with caplog.at_level(logging.WARNING, logger=queue_mod.logger.name):
            resumed = queue_mod.recover_pending_jobs()

    assert resumed == 50
    assert mock_submit.call_count == 50, "every job must still be resubmitted - behavior unchanged"

    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1, (
        f"expected exactly 1 aggregate warning for 50 resumed jobs, "
        f"got {len(warning_records)}"
    )
    message = warning_records[0].getMessage()
    assert "50" in message
    # The message is capped, not an unbounded dump of every id.
    assert "more" in message


def test_recovering_zero_jobs_logs_nothing(sqlite_session_factory, caplog):
    with caplog.at_level(logging.WARNING, logger=queue_mod.logger.name):
        resumed = queue_mod.recover_pending_jobs()
    assert resumed == 0
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 0


# ---------------------------------------------------------------------
# 4. Important errors are still logged (requirement: do not remove
#    useful error/warning logs).
# ---------------------------------------------------------------------

def test_leader_election_worker_crash_is_still_logged_as_an_error(caplog):
    """This app's own genuine error logging (a background worker
    crash) must be untouched by this pass."""
    import asyncio
    from app.simulator.leader_election import LeaderElection

    async def crashing_loop():
        raise RuntimeError("simulated crash for logging test")

    async def scenario():
        with patch(
            "app.simulator.leader_election.cache.redis_status",
            return_value={"connected": False, "state": "disabled"},
        ):
            election = LeaderElection("logging_regression_test", crashing_loop)
            with caplog.at_level(logging.ERROR):
                await election._election_tick()
                worker_task = election._worker_task
                try:
                    await worker_task
                except RuntimeError:
                    pass
            # Not calling election.stop() here: the worker already
            # crashed and finished on its own (nothing left running to
            # clean up), and this test only needs to confirm the crash
            # was logged as an ERROR - unrelated to this logging-only
            # pass's scope.

    asyncio.run(scenario())

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("worker crashed" in r.getMessage() for r in error_records), (
        "a genuine background-job crash must still produce an ERROR log"
    )


def test_health_check_db_failure_is_still_logged_as_an_error(caplog, monkeypatch):
    from app.api.v1 import health as health_module

    class _BoomEngine:
        def connect(self):
            raise RuntimeError("db is down")

    monkeypatch.setattr(health_module, "engine", _BoomEngine())

    with caplog.at_level(logging.ERROR):
        result = health_module.health_check()

    assert result["database"] == "error"
    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("database check failed" in r.getMessage() for r in error_records)


# ---------------------------------------------------------------------
# 5. Existing simulator/tracker/API/WebSocket behavior is unaffected -
#    the fixes above touched no timing, no routes, no payload shape.
#    (Full coverage already lives in test_simulator_scheduler_intervals.py,
#    test_leader_election*.py, and the API/WebSocket suites; this is a
#    direct spot-check that intervals specifically are untouched by
#    this logging-only pass.)
# ---------------------------------------------------------------------

def test_simulator_and_tracker_intervals_config_defaults_unchanged():
    assert settings.SIMULATOR_INTERVAL_SECONDS == 60
    assert settings.TRAIN_TRACK_INTERVAL_SECONDS == 60
