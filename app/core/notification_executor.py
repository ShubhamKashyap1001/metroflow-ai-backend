"""Dedicated worker pool for outbound email/SMS notification dispatch.

WHY THIS EXISTS (Phase 8 - see docs/notification-delivery.md)
-------------------------------------------------------
Before this module, alert dispatch was scheduled with FastAPI's
`BackgroundTasks.add_task(...)`. That does get the *triggering*
request its response immediately - but Starlette runs a sync
background task via `starlette.concurrency.run_in_threadpool`, which
hands it to AnyIO's *default worker thread limiter*. That is the exact
same shared, capacity-limited thread pool (`anyio.to_thread`, default
40 tokens) that every plain `def` route handler in this app also runs
on - and nearly every route here is a sync `def`, not `async def`
(list_alerts, get_alerts, crowd reads, check-ins, admin actions, ...).

`app/core/email.py`/`app/core/sms.py` are deliberately slow: one
blocking SMTP send or Twilio HTTP POST per recipient, sequentially,
each with its own `timeout=15`. A handful of alerts raised close
together (or one alert with many recipients) can occupy several of
those 40 shared slots for seconds at a time. Once enough of them are
in flight, *unrelated* API requests - which also need a slot from that
same pool just to run their own (fast) handler - start queuing behind
the slow notification sends. That is still "email/SMS blocking API
requests" even though the request that *triggered* the dispatch
already got its response; the effect lands on every *other* concurrent
request instead. Measured locally (see docs/notification-delivery.md): a burst
of 45 concurrent alert dispatches drove concurrent `GET /alerts`
latency from ~10ms to over 20s, with both endpoints eventually
returning 503 (the pool-exhaustion handler in app/main.py firing for
reasons that have nothing to do with the DB pool itself).

THE FIX
-------
Give notification dispatch its own small, bounded `ThreadPoolExecutor`
- completely separate from AnyIO's worker limiter. A burst of slow
notification sends can now only ever compete with *other* notification
dispatches for a thread, never with the pool every other endpoint
depends on. `NOTIFICATION_DISPATCH_WORKERS` (default 8) caps how many
dispatch batches can run at once; further submissions queue on this
executor's own internal queue instead of stealing capacity from
anything else.

This module intentionally has zero dependency on FastAPI/Starlette/
AnyIO - it's a plain `concurrent.futures.ThreadPoolExecutor`, submitted
to directly from the sync route handler instead of going through
`BackgroundTasks`.
"""
import atexit
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.core.config import settings

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(
    max_workers=settings.NOTIFICATION_DISPATCH_WORKERS,
    thread_name_prefix="notify-dispatch",
)

def submit(fn: Callable, *args, **kwargs) -> None:
    """Fire-and-forget a notification dispatch job on the dedicated
    pool.

    Never raises back to the caller (a route handler that has likely
    already built its response) - a failure to even *submit* the job
    (e.g. the executor is mid-shutdown) is logged, not propagated, the
    same "never take the request down" contract
    send_alert_emails/send_alert_sms already follow for per-recipient
    failures.
    """
    def _run() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Notification dispatch job failed: %s", getattr(fn, "__name__", fn))

    try:
        _executor.submit(_run)
    except RuntimeError:
        logger.error(
            "Notification dispatch executor is not accepting new work "
            "(shutdown in progress) - dropped a %s job.",
            getattr(fn, "__name__", fn),
        )

def shutdown(wait: bool = True) -> None:
    """Called from app/main.py's lifespan on shutdown. `wait=True`
    lets in-flight sends finish (bounded by their own 15s socket
    timeouts) instead of abandoning them mid-send."""
    _executor.shutdown(wait=wait)

# Best-effort safety net if the app process exits without running the
# lifespan shutdown block (e.g. `python -c` scripts, a hard kill signal
# uvicorn still forwards, test runners that import the app directly).
atexit.register(shutdown, wait=False)
