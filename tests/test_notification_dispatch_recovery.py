from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.enums.notification_channel import NotificationChannel
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.notification_dispatch_status import NotificationDispatchStatus
from app.enums.notification_status import NotificationStatus
from app.models.alert import Alert
from app.models.notification_dispatch_job import NotificationDispatchJob
from app.models.notification_log import NotificationLog
from app.models.station import Station
from app.models.user_profile import UserProfile
from app.services import alert_service
from app.services import notification_dispatch_queue as queue_mod


@pytest.fixture
def sqlite_session_factory(monkeypatch):
    """A real, isolated in-memory SQLite engine with just the
    notification_dispatch_jobs table created - swapped in for
    queue_mod.SessionLocal so the module under test does genuine
    ORM read/write/commit round trips without needing Postgres."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine, tables=[NotificationDispatchJob.__table__])
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(queue_mod, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def _get(session_factory, job_id):
    db = session_factory()
    try:
        return db.get(NotificationDispatchJob, job_id)
    finally:
        db.close()


def _insert(session_factory, **kwargs):
    db = session_factory()
    try:
        job = NotificationDispatchJob(**kwargs)
        db.add(job)
        db.commit()
        db.refresh(job)
        return job.id
    finally:
        db.close()


# ---------------------------------------------------------------------
# Fix #1: a job is durably persisted before it ever reaches the
# in-memory executor - so a restart before/while it's only in that
# in-memory queue can't silently lose it.
# ---------------------------------------------------------------------


def test_enqueue_persists_job_before_submitting_to_executor(sqlite_session_factory):
    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        queue_mod.enqueue_and_submit(
            NotificationDispatchKind.ALERT_CREATED,
            alert_id=7,
            actor_id="user-1",
            notify_email=True,
            notify_sms=False,
        )

    # The row exists, committed, as QUEUED - independent of whatever
    # the executor does with it.
    db = sqlite_session_factory()
    try:
        jobs = db.query(NotificationDispatchJob).all()
    finally:
        db.close()
    assert len(jobs) == 1
    assert jobs[0].status == NotificationDispatchStatus.QUEUED
    assert jobs[0].alert_id == 7
    assert jobs[0].attempts == 0

    # And it WAS handed to the dedicated pool, not run inline.
    mock_submit.assert_called_once()
    assert mock_submit.call_args[0][0] is queue_mod.run_job
    assert mock_submit.call_args[0][1] == jobs[0].id


def test_job_surviving_only_in_memory_would_be_lost_without_the_row(sqlite_session_factory):
    """Sanity check on the failure mode being fixed: if submission to
    the executor fails/never happens (simulating a crash right after
    enqueue), the durable row still exists and is exactly what
    recover_pending_jobs() will find - nothing about recovery depends
    on the executor call having succeeded."""
    with patch.object(queue_mod.notification_executor, "submit", side_effect=RuntimeError("process died")):
        with pytest.raises(RuntimeError):
            queue_mod.enqueue_and_submit(
                NotificationDispatchKind.ALERT_CREATED,
                alert_id=9,
                actor_id=None,
                notify_email=True,
                notify_sms=True,
            )

    db = sqlite_session_factory()
    try:
        job = db.query(NotificationDispatchJob).filter_by(alert_id=9).one()
    finally:
        db.close()
    assert job.status == NotificationDispatchStatus.QUEUED


# ---------------------------------------------------------------------
# Fix #2: failure -> restart -> recovery, with attempts as durable
# retry state.
# ---------------------------------------------------------------------


def test_run_job_success_marks_done_and_records_attempt(sqlite_session_factory):
    job_id = _insert(
        sqlite_session_factory,
        kind=NotificationDispatchKind.ALERT_CREATED,
        alert_id=1,
        actor_id="user-1",
        notify_email=True,
        notify_sms=False,
        status=NotificationDispatchStatus.QUEUED,
        attempts=0,
    )

    with patch.object(queue_mod.alert_service, "dispatch_alert_notifications") as mock_dispatch:
        queue_mod.run_job(job_id)

    mock_dispatch.assert_called_once_with(1, "user-1", True, False, job_id=job_id)
    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.DONE
    assert job.attempts == 1
    assert job.completed_at is not None


def test_crash_mid_job_then_restart_recovers_and_completes(sqlite_session_factory):
    """The full failure -> restart -> recovery cycle:

    1. A job starts running (IN_PROGRESS, attempts=1) - then the
       process is killed before it can mark DONE/FAILED. Nothing about
       a real SIGKILL calls any more of our code, so this is modeled
       exactly as it would look on disk afterwards: an IN_PROGRESS row
       stuck at attempts=1.
    2. The process restarts. recover_pending_jobs() finds that row and
       resubmits it.
    3. This time the dispatch succeeds, and the job reaches DONE with
       attempts=2 - one attempt lost to the crash, one that completed
       it, both reflected in the durable `attempts` counter.
    """
    job_id = _insert(
        sqlite_session_factory,
        kind=NotificationDispatchKind.ALERT_CREATED,
        alert_id=2,
        actor_id="user-2",
        notify_email=True,
        notify_sms=True,
        status=NotificationDispatchStatus.IN_PROGRESS,
        attempts=1,
    )

    # Step 2: restart -> recovery finds the stuck job and resubmits it.
    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        resumed = queue_mod.recover_pending_jobs()
    assert resumed == 1
    mock_submit.assert_called_once_with(queue_mod.run_job, job_id)

    # It's still IN_PROGRESS at this point - recovery only resubmits,
    # it doesn't run the job itself.
    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.IN_PROGRESS
    assert job.attempts == 1

    # Step 3: the resubmitted job actually runs (what the executor
    # would have done) and this time succeeds.
    with patch.object(queue_mod.alert_service, "dispatch_alert_notifications") as mock_dispatch:
        queue_mod.run_job(job_id)
    mock_dispatch.assert_called_once_with(2, "user-2", True, True, job_id=job_id)

    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.DONE
    assert job.attempts == 2, "one attempt from before the crash, one from the recovered run"


def test_queued_job_never_submitted_is_also_recovered(sqlite_session_factory):
    """Covers the OTHER half of "lost on restart": a job that never
    even got IN_PROGRESS (the process died before a worker thread
    picked it up, or while it sat in the executor's own internal
    queue) is still QUEUED, and recovery treats it the same as an
    IN_PROGRESS one."""
    job_id = _insert(
        sqlite_session_factory,
        kind=NotificationDispatchKind.ALERT_RESOLVED,
        alert_id=3,
        actor_id=None,
        notify_email=False,
        notify_sms=False,
        status=NotificationDispatchStatus.QUEUED,
        attempts=0,
    )

    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        resumed = queue_mod.recover_pending_jobs()
    assert resumed == 1
    mock_submit.assert_called_once_with(queue_mod.run_job, job_id)


def test_recovery_gives_up_after_max_attempts_instead_of_looping_forever(sqlite_session_factory, monkeypatch):
    """A job that keeps crashing the process every single time it runs
    must not be retried forever - once it hits
    NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS, recovery marks it FAILED
    and does NOT resubmit it again."""
    monkeypatch.setattr(queue_mod.settings, "NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS", 3)
    job_id = _insert(
        sqlite_session_factory,
        kind=NotificationDispatchKind.ALERT_CREATED,
        alert_id=4,
        actor_id=None,
        notify_email=True,
        notify_sms=False,
        status=NotificationDispatchStatus.IN_PROGRESS,
        attempts=3,
    )

    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        resumed = queue_mod.recover_pending_jobs()

    assert resumed == 0
    mock_submit.assert_not_called()
    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.FAILED
    assert job.completed_at is not None
    assert "3 attempts" in job.last_error


def test_run_job_failure_marks_failed_but_does_not_crash_caller(sqlite_session_factory):
    """A real (non-crash) exception during dispatch still surfaces
    (notification_executor's own wrapper is what swallows it in
    production - see app/core/notification_executor.py), but the job
    row is left in a clean FAILED state with the error recorded, not
    stuck IN_PROGRESS forever."""
    job_id = _insert(
        sqlite_session_factory,
        kind=NotificationDispatchKind.ALERT_CREATED,
        alert_id=5,
        actor_id=None,
        notify_email=True,
        notify_sms=False,
        status=NotificationDispatchStatus.QUEUED,
        attempts=0,
    )

    with patch.object(
        queue_mod.alert_service, "dispatch_alert_notifications", side_effect=RuntimeError("smtp exploded")
    ):
        with pytest.raises(RuntimeError):
            queue_mod.run_job(job_id)

    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.FAILED
    assert job.last_error == "smtp exploded"
    assert job.attempts == 1


@pytest.fixture
def full_schema_session_factory(monkeypatch):
    """A second in-memory SQLite engine, this one with the full set of
    tables `alert_service._dispatch` actually touches (stations,
    user_profiles, alerts, notification_dispatch_jobs,
    notification_logs) - swapped in for BOTH `queue_mod.SessionLocal`
    and `alert_service.SessionLocal` so `run_job` -> `alert_service`
    round trips through real ORM read/write/commit, exactly like
    production, with only the outbound email network call mocked."""
    import uuid

    from app.enums.alert_type import AlertType
    from app.models.notification import Notification

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Station.__table__,
            UserProfile.__table__,
            Alert.__table__,
            NotificationDispatchJob.__table__,
            NotificationLog.__table__,
            Notification.__table__,
        ],
    )
    TestSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(queue_mod, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(alert_service, "SessionLocal", TestSessionLocal)

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

        users = [
            UserProfile(id=uuid.uuid4(), email="alice@example.com", full_name="Alice", is_active=True),
            UserProfile(id=uuid.uuid4(), email="bob@example.com", full_name="Bob", is_active=True),
        ]
        db.add_all(users)

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


def test_recovered_job_does_not_resend_to_already_notified_recipients(full_schema_session_factory):
    """Regression for the duplicate-dispatch bug: a job resumed after a
    crash must not re-send email/SMS to a recipient it already
    successfully reached under that same job_id - only recipients not
    yet confirmed sent should be (re)attempted, so retries still work.

    Models a crash that happens AFTER the email to alice@ was sent and
    logged, but BEFORE the job row could be marked DONE: exactly the
    "IN_PROGRESS with a partial NotificationLog trail" state
    `recover_pending_jobs()` finds and resubmits.
    """
    TestSessionLocal, alert_id = full_schema_session_factory

    job_id = _insert(
        TestSessionLocal,
        kind=NotificationDispatchKind.ALERT_CREATED,
        alert_id=alert_id,
        actor_id=None,
        notify_email=True,
        notify_sms=False,
        status=NotificationDispatchStatus.IN_PROGRESS,
        attempts=1,
    )

    # The crashed first attempt got as far as actually emailing (and
    # logging) alice@ before the process died mid-job.
    db = TestSessionLocal()
    try:
        db.add(
            NotificationLog(
                alert_id=alert_id,
                job_id=job_id,
                channel=NotificationChannel.EMAIL,
                recipient="alice@example.com",
                status=NotificationStatus.SENT,
            )
        )
        db.commit()
    finally:
        db.close()

    with patch.object(alert_service, "send_alert_emails") as mock_send_emails:
        mock_send_emails.return_value = {"bob@example.com": "sent"}
        # This is what recovery resubmitting the job to
        # notification_executor ultimately runs.
        queue_mod.run_job(job_id)

    # Only the recipient NOT already confirmed sent under this job_id
    # was handed to the email provider - alice@ is excluded even
    # though the whole job re-ran from scratch.
    mock_send_emails.assert_called_once()
    sent_recipients = set(mock_send_emails.call_args.kwargs.get("recipients") or mock_send_emails.call_args[1].get("recipients") or mock_send_emails.call_args[0][0])
    assert sent_recipients == {"bob@example.com"}
    assert "alice@example.com" not in sent_recipients

    # The job completed normally, and NotificationLog now shows exactly
    # one SENT row per recipient for this job - no duplicates.
    db = TestSessionLocal()
    try:
        job = db.get(NotificationDispatchJob, job_id)
        logs = (
            db.query(NotificationLog)
            .filter(NotificationLog.job_id == job_id, NotificationLog.channel == NotificationChannel.EMAIL)
            .all()
        )
    finally:
        db.close()

    assert job.status == NotificationDispatchStatus.DONE
    recipients_logged = [log.recipient for log in logs]
    assert sorted(recipients_logged) == ["alice@example.com", "bob@example.com"]
    assert len(recipients_logged) == len(set(recipients_logged)), "no duplicate NotificationLog rows for this job"


def test_dispatch_recipient_lookup_selects_only_email_and_phone_columns(full_schema_session_factory):
    """RAM FIX (Render Free 512MB): alert_service._dispatch resolves its
    email/SMS recipient set by querying UserProfile - a table that grows
    with the real (non-simulated) user base and is queried fresh on
    every single alert create/resolve, not just once. It only ever
    reads `.email`/`.phone` off each row, so the query must select just
    those two columns instead of hydrating a full UserProfile ORM
    instance (id, full_name, username, avatar_url, role, timestamps,
    etc.) per active user - the same "fetch only required columns"
    fix already applied to train_tracking.py's route-cache query.

    Verified functionally (not just by inspecting the query object):
    patch Session.query itself and assert every call site that reads
    from UserProfile requests only (UserProfile.email, UserProfile.phone),
    never the bare UserProfile entity - while still confirming the
    recipient resolution behaves identically (right emails reached)."""
    TestSessionLocal, alert_id = full_schema_session_factory

    from sqlalchemy.orm import Session as _Session

    queried_entities = []
    original_query = _Session.query

    def _tracking_query(self, *entities, **kwargs):
        queried_entities.append(entities)
        return original_query(self, *entities, **kwargs)

    with patch.object(_Session, "query", _tracking_query):
        with patch.object(alert_service, "send_alert_emails") as mock_send_emails:
            mock_send_emails.return_value = {
                "alice@example.com": "sent",
                "bob@example.com": "sent",
            }
            alert_service.dispatch_alert_notifications(alert_id, None, True, False)

    # The two real recipients were still resolved correctly...
    mock_send_emails.assert_called_once()
    sent_recipients = set(
        mock_send_emails.call_args.kwargs.get("recipients")
        or mock_send_emails.call_args[0][0]
    )
    assert sent_recipients == {"alice@example.com", "bob@example.com"}

    # ...but no call site ever asked for the whole UserProfile entity -
    # only the (email, phone) column pair.
    user_profile_calls = [
        entities for entities in queried_entities
        if entities and entities[0] is UserProfile
    ]
    assert not user_profile_calls, (
        "a full UserProfile row was queried instead of selecting only "
        "the email/phone columns actually used"
    )
    column_only_calls = [
        entities for entities in queried_entities
        if entities and entities[0] is UserProfile.email
    ]
    assert column_only_calls, "expected at least one (UserProfile.email, UserProfile.phone) query"
    assert column_only_calls[0] == (UserProfile.email, UserProfile.phone)


def test_run_job_is_a_no_op_if_already_done(sqlite_session_factory):
    """If recovery resubmits a job right as its original in-flight run
    finishes (a narrow race, not the common case), the resubmitted run
    must not re-dispatch and double-send."""
    job_id = _insert(
        sqlite_session_factory,
        kind=NotificationDispatchKind.ALERT_CREATED,
        alert_id=6,
        actor_id=None,
        notify_email=True,
        notify_sms=False,
        status=NotificationDispatchStatus.DONE,
        attempts=1,
    )

    with patch.object(queue_mod.alert_service, "dispatch_alert_notifications") as mock_dispatch:
        queue_mod.run_job(job_id)

    mock_dispatch.assert_not_called()
    job = _get(sqlite_session_factory, job_id)
    assert job.attempts == 1
