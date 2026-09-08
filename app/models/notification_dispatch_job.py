"""Phase 11: durable notification dispatch queue.

WHY THIS EXISTS
----------------
`app/core/notification_executor.py` gives alert email/SMS dispatch its
own thread pool so it can't block unrelated API requests (see
docs/notification-delivery.md) - but everything submitted to it lives
only in that `ThreadPoolExecutor`'s in-memory queue/worker threads.
Nothing about it survives the process going away: a deploy, a crash, a
hard kill (`SIGKILL`, an out-of-memory reaper) at the wrong moment
loses any job still sitting in the executor's internal queue *and* any
job a worker thread was already partway through sending. Neither case
raises an exception anywhere - the notification is just never sent,
silently.

THE FIX
-------
Before a dispatch job is ever handed to `notification_executor.submit`,
`app/services/notification_dispatch_queue.py` first writes and commits
one of these rows with `status=QUEUED`. The executor callback then
flips it to `IN_PROGRESS` (recording an attempt) before doing any
actual work, and to `DONE`/`FAILED` when it finishes. On startup,
`recover_pending_jobs()` looks for any row still `QUEUED` or
`IN_PROGRESS` - by definition, a job in either state with no process
currently running it means the process that owned it went away before
it finished - and resubmits it, up to
`NOTIFICATION_DISPATCH_MAX_JOB_ATTEMPTS` attempts. `attempts` is the
"retry state" that survives the restart: it's what lets recovery tell
"just queued, try it" apart from "we've already tried this N times and
it keeps dying, stop".

This intentionally re-runs the whole job from scratch rather than
resuming mid-recipient-list, but stays duplicate-safe by having every
`NotificationLog` row record which job produced it (`job_id`) and
having `alert_service._dispatch` skip any recipient that already has a
SENT log row for THIS job/channel before it ever calls out to the
email/SMS provider again. A job resumed after a crash therefore can
retry recipients it never reached or that failed, without re-sending
to anyone it already reached - "silently never sent" is fixed without
reintroducing "sent twice", and without restructuring the
per-recipient send/log logic itself.
"""
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String

from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.notification_dispatch_status import NotificationDispatchStatus
from app.mixins.timestamp import TimestampMixin

class NotificationDispatchJob(TimestampMixin, Base):

    __tablename__ = "notification_dispatch_jobs"

    # Recovery's whole query is "find the stuck ones" - WHERE status IN
    # (QUEUED, IN_PROGRESS) - on startup, so it needs to be fast even
    # once this table has accumulated a long history of DONE/FAILED
    # rows from every alert ever dispatched.
    __table_args__ = (
        Index("ix_notification_dispatch_jobs_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    kind: Mapped[NotificationDispatchKind] = mapped_column(
        Enum(NotificationDispatchKind)
    )

    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id")
    )

    # created_by_id (ALERT_CREATED) or resolved_by_id (ALERT_RESOLVED) -
    # whichever actor triggered this dispatch. Stored as plain text
    # (not a FK) since user_profiles.id is a UUID and this is only ever
    # read back to pass straight through to the same dispatch functions
    # that already accept it as `str | None`.
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    notify_email: Mapped[bool] = mapped_column(default=False)
    notify_sms: Mapped[bool] = mapped_column(default=False)

    status: Mapped[NotificationDispatchStatus] = mapped_column(
        Enum(NotificationDispatchStatus),
        default=NotificationDispatchStatus.QUEUED,
    )

    # Incremented once per attempt (including the very first), BEFORE
    # the dispatch function runs - so this is durable "how many times
    # has this been tried" state that survives the process dying
    # mid-attempt, which is exactly the scenario an in-memory-only
    # counter can't cover.
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
