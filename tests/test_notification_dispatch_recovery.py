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

def test_enqueue_persists_job_before_submitting_to_executor(sqlite_session_factory):
    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        queue_mod.enqueue_and_submit(
            NotificationDispatchKind.ALERT_CREATED,
            alert_id=7,
            actor_id="user-1",
            notify_email=True,
            notify_sms=False,
        )

    db = sqlite_session_factory()
    try:
        jobs = db.query(NotificationDispatchJob).all()
    finally:
        db.close()
    assert len(jobs) == 1
    assert jobs[0].status == NotificationDispatchStatus.QUEUED
    assert jobs[0].alert_id == 7
    assert jobs[0].attempts == 0

    mock_submit.assert_called_once()
    assert mock_submit.call_args[0][0] is queue_mod.run_job
    assert mock_submit.call_args[0][1] == jobs[0].id

def test_job_surviving_only_in_memory_would_be_lost_without_the_row(sqlite_session_factory):
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

    with patch.object(queue_mod.notification_executor, "submit") as mock_submit:
        resumed = queue_mod.recover_pending_jobs()
    assert resumed == 1
    mock_submit.assert_called_once_with(queue_mod.run_job, job_id)

    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.IN_PROGRESS
    assert job.attempts == 1

    with patch.object(queue_mod.alert_service, "dispatch_alert_notifications") as mock_dispatch:
        queue_mod.run_job(job_id)
    mock_dispatch.assert_called_once_with(2, "user-2", True, True, job_id=job_id)

    job = _get(sqlite_session_factory, job_id)
    assert job.status == NotificationDispatchStatus.DONE
    assert job.attempts == 2, "one attempt from before the crash, one from the recovered run"

def test_queued_job_never_submitted_is_also_recovered(sqlite_session_factory):
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

        queue_mod.run_job(job_id)

    mock_send_emails.assert_called_once()
    sent_recipients = set(mock_send_emails.call_args.kwargs.get("recipients") or mock_send_emails.call_args[1].get("recipients") or mock_send_emails.call_args[0][0])
    assert sent_recipients == {"bob@example.com"}
    assert "alice@example.com" not in sent_recipients

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

    mock_send_emails.assert_called_once()
    sent_recipients = set(
        mock_send_emails.call_args.kwargs.get("recipients")
        or mock_send_emails.call_args[0][0]
    )
    assert sent_recipients == {"alice@example.com", "bob@example.com"}

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
