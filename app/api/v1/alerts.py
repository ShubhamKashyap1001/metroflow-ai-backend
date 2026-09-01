"""Alert & Notification Module. Overcrowding / delay / emergency /
maintenance / info alerts, raised by admins and operators, station-wise
filterable, with a resolve workflow. Every alert now also dispatches
an email + SMS (in the background, so the API responds instantly) to
every active user with an email/phone on file, plus an explicit copy
to whoever raised it - see app/core/email.py, app/core/sms.py and
app/services/alert_service.py.

Two extra behaviours on top of that:
  - AlertCreate accepts an optional `available_until` ("service
    expected back by 9:00 PM") that's included in the dispatched
    email/SMS and shown on the alert card.
  - Resolving an alert (PATCH /{id}/resolve) re-notifies the same
    audience, on the same channel(s) it was originally raised on, that
    the issue is now resolved - unless the caller explicitly opts out
    with notify_on_resolve=false.

Phase 8: dispatch is submitted to app/core/notification_executor.py's
own dedicated thread pool, NOT FastAPI's BackgroundTasks - see that
module's docstring and docs/notification-delivery.md for why (BackgroundTasks
shares the same worker pool every sync route handler runs on, so a
burst of slow email/SMS sends could still delay unrelated API
requests even though the triggering request itself already got its
response).

Phase 11: the actual submission to that thread pool now goes through
app/services/notification_dispatch_queue.py, not
notification_executor.submit() directly - it writes a durable DB row
for the job first, so a process restart while the dispatch is queued
or mid-flight can find and resume it instead of silently losing it.
See app/models/notification_dispatch_job.py.
"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import get_current_user, require_roles
from app.database.session import get_db
from app.enums.notification_dispatch_kind import NotificationDispatchKind
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.alert import AlertCreate, AlertResolve, AlertResponse
from app.schemas.notification_log import NotificationLogResponse
from app.services import alert_service
from app.services import notification_dispatch_queue

router = APIRouter(
    prefix="/alerts",
    tags=["Alerts"]
)

@router.get("/", response_model=list[AlertResponse])
def get_alerts(
    station_id: int | None = None,
    active_only: bool = False,
    state: str | None = None,
    limit: int = alert_service.DEFAULT_ALERTS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    """`limit`/`offset` page through results, most recent first (default
    page size and hard cap enforced in alert_service.list_alerts, so an
    out-of-range value here is clamped rather than rejected)."""
    return alert_service.list_alerts(db, station_id, active_only, state, limit, offset)

@router.post("/", response_model=AlertResponse, status_code=201)
@limiter.limit(WRITE_LIMIT)
def create_alert(
    request: Request,
    payload: AlertCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    alert = alert_service.create_alert(db, payload, created_by=current_user.id)

    if payload.notify_email or payload.notify_sms:
        # Dedicated pool (Phase 8), via the durable queue (Phase 11) -
        # never the shared request-handling thread pool. Fire-and-
        # forget: this call returns immediately (after a fast, already-
        # committed DB write), the actual email/SMS sends happen on
        # notification_executor's own worker threads.
        notification_dispatch_queue.enqueue_and_submit(
            NotificationDispatchKind.ALERT_CREATED,
            alert.id,
            current_user.id,
            payload.notify_email,
            payload.notify_sms,
        )

    return alert

@router.get("/{alert_id}", response_model=AlertResponse)
def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return alert_service.get_alert(db, alert_id)

@router.patch("/{alert_id}/resolve", response_model=AlertResponse)
@limiter.limit(WRITE_LIMIT)
def resolve_alert(
    request: Request,
    alert_id: int,
    payload: AlertResolve = AlertResolve(),
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    alert, just_resolved = alert_service.resolve_alert(db, alert_id)

    # Phase 10: only dispatch on the call that actually resolved the
    # alert. Gating on `notify_on_resolve` alone made this endpoint
    # non-idempotent - a retried/duplicated PATCH (dropped response,
    # double-click, client retry-on-timeout) for an already-resolved
    # alert would re-send the resolution email/SMS/bell notification
    # every time it was called. See alert_service.resolve_alert and
    # docs/notification-delivery.md.
    if just_resolved and payload.notify_on_resolve:
        # Dedicated pool (Phase 8), via the durable queue (Phase 11) -
        # see create_alert above.
        notification_dispatch_queue.enqueue_and_submit(
            NotificationDispatchKind.ALERT_RESOLVED,
            alert.id,
            current_user.id,
            notify_email=False,
            notify_sms=False,
        )

    return alert

@router.get("/{alert_id}/notifications", response_model=list[NotificationLogResponse])
def get_alert_notifications(
    alert_id: int,
    limit: int = alert_service.DEFAULT_ALERT_NOTIFICATIONS_LIMIT,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    """Per-recipient delivery status for this alert's email + SMS
    dispatch - who was notified on which channel, who failed, and
    why. `limit`/`offset` page through results (clamped server-side -
    see alert_service.list_alert_notifications) since a single alert
    sent to a large active-user base can generate one row per
    (recipient, channel)."""
    return alert_service.list_alert_notifications(db, alert_id, limit, offset)
