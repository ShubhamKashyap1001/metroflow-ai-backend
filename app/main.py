import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.rate_limit import _rate_limit_exceeded_handler_with_retry_after
from sqlalchemy.exc import TimeoutError as SATimeoutError

logger = logging.getLogger(__name__)

from app.core import log_buffer, metrics
# Attach the in-memory log ring buffer as early as possible so it
# captures startup-time log records too (model warmup, simulator
# boot, etc.), not just requests handled after the app is "ready".
log_buffer.install()

from app.api.v1 import (
    admin,
    alerts,
    analytics,
    authentication,
    chatbot,
    checkin,
    checkout,
    crowd,
    enquiries,
    health,
    meta,
    news,
    notifications,
    prediction,
    schedule,
    station,
    trains,
    users,
)
from app.core import notification_executor
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import get_user_from_token_optional
from app.database.session import SessionLocal
from app.enums.notification_source import NotificationSource
from app.services import notification_dispatch_queue
from app.services import notification_service
from app.simulator.scheduler import (
    start_notification_bin_retention_job,
    start_retention_job,
    start_simulator,
    start_train_tracker,
    stop_notification_bin_retention_job,
    stop_retention_job,
    stop_simulator,
    stop_train_tracker,
)
from app.websocket.manager import manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.bind_loop(asyncio.get_running_loop())
    # Subscribe THIS process to the cross-process WebSocket
    # relay so its own clients get every event - simulator ticks AND
    # request-triggered alerts/notifications/delays/check-in updates -
    # even when a different worker process generated it (see
    # app/websocket/manager.py's start_relay/broadcast and
    # app/simulator/leader_election.py). No-op if Redis is unavailable.
    manager.start_relay()
    # Proactively reap WebSocket connections that have gone
    # silent (network interruption, sleeping laptop, etc.) instead of
    # only noticing them the next time a broadcast happens to try (and
    # fail) to send to them. See ConnectionManager.start_reaper.
    manager.start_reaper()

    # Load the 3 .pkl model bundles now, during startup, instead of
    # letting the first real prediction request pay that cost (see
    # app/ai_engine/warmup.py). Runs in a worker thread so a slow disk
    # read can't block the event loop from coming up.
    from app.ai_engine.warmup import warm_up_models
    await asyncio.to_thread(warm_up_models)

    # Phase 11: resume any alert email/SMS dispatch a previous process
    # left QUEUED or IN_PROGRESS (deploy, crash, hard kill) before this
    # process starts serving traffic - see
    # app/services/notification_dispatch_queue.py and
    # app/models/notification_dispatch_job.py. Runs in a worker thread
    # since it does blocking DB I/O.
    resumed = await asyncio.to_thread(notification_dispatch_queue.recover_pending_jobs)
    if resumed:
        logger.warning(
            "Resumed %d notification dispatch job(s) left over from a previous process.",
            resumed,
        )

    tick = settings.SIMULATOR_INTERVAL_SECONDS
    if settings.ENABLE_SIMULATOR:
        start_simulator(SessionLocal, tick)
    if settings.ENABLE_TRAIN_TRACKING:
        start_train_tracker(SessionLocal, tick)
    if settings.ENABLE_CROWD_RETENTION_JOB:
        start_retention_job(SessionLocal, settings.CROWD_RETENTION_INTERVAL_SECONDS)
    if settings.ENABLE_NOTIFICATION_BIN_RETENTION_JOB:
        start_notification_bin_retention_job(
            SessionLocal, settings.NOTIFICATION_BIN_RETENTION_INTERVAL_SECONDS
        )
    yield
    if settings.ENABLE_SIMULATOR:
        await stop_simulator()
    if settings.ENABLE_TRAIN_TRACKING:
        await stop_train_tracker()
    if settings.ENABLE_CROWD_RETENTION_JOB:
        await stop_retention_job()
    if settings.ENABLE_NOTIFICATION_BIN_RETENTION_JOB:
        await stop_notification_bin_retention_job()
    manager.stop_relay()
    await manager.stop_reaper()
    # Let any in-flight email/SMS dispatch finish (bounded by
    # their own 15s socket timeouts) rather than abandoning it
    # mid-send. Runs after everything else so it doesn't delay the
    # rest of shutdown on a quiet process (nothing in flight = returns
    # immediately).
    notification_executor.shutdown(wait=True)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler_with_retry_after)
# Required for `limiter`'s default_limits (and any @limiter.limit(...) on a
# route) to actually be enforced - see app/core/rate_limit.py's module
# docstring. This is a BaseHTTPMiddleware subclass, so it only wraps "http"
# scope requests; the "/ws/monitor" WebSocket route below is untouched by it.
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(SATimeoutError)
async def db_pool_exhausted_handler(request: Request, exc: SATimeoutError):
    """The DB connection pool (DB_POOL_SIZE + DB_MAX_OVERFLOW) had no
    free connection within DB_POOL_TIMEOUT seconds. Without this
    handler, that exception was unhandled: it printed a full traceback
    to the terminal on every occurrence and returned a bare 500, and
    because it took the full pool_timeout to surface, requests piled
    up behind it, making the whole app look frozen/crashed under load
    instead of a single endpoint failing cleanly. This logs it once
    (no traceback spam) and returns a fast, clear 503 the frontend's
    existing retry logic already knows how to handle."""
    logger.error("[db] connection pool exhausted on %s - consider raising DB_POOL_SIZE/DB_MAX_OVERFLOW "
                  "or checking for a slow query/unreachable DB.", request.url.path)
    # A pool checkout timeout never reaches a DBAPI call, so it never
    # fires database.py's `handle_error` engine event - this is the
    # only place it's ever recorded.
    metrics.record_db_failure("pool_exhausted")

    try:
                                                                    
        notif_db = SessionLocal()
        try:
            notification_service.create_notification(
                notif_db,
                source=NotificationSource.SYSTEM_FAILURE,
                title="Database connection pool exhausted",
                message=f"No free DB connection within the pool timeout on {request.url.path}. "
                        "Consider raising DB_POOL_SIZE/DB_MAX_OVERFLOW or checking for a slow query.",
            )
        finally:
            notif_db.close()
    except Exception:
                                                                     
        pass

    return JSONResponse(
        status_code=503,
        content={"detail": "Server is busy, please try again in a moment."},
    )

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.error(
        "[unhandled] %s %s -> %s: %s",
        request.method,
        request.url.path,
        type(exc).__name__,
        exc,
        exc_info=exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again later."},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    if request.url.path == "/metrics":
        # Don't record scrapes of the metrics endpoint itself.
        return await call_next(request)

    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        duration_seconds = time.perf_counter() - start
        route = metrics.route_label(request)
        metrics.HTTP_REQUESTS_TOTAL.labels(
            method=request.method, route=route, status=str(status_code)
        ).inc()
        metrics.HTTP_REQUEST_DURATION_SECONDS.labels(
            method=request.method, route=route
        ).observe(duration_seconds)

@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    """Prometheus scrape target - no auth, same as /health, matching
    how Prometheus itself scrapes (no easy way for it to send a Bearer
    token) and how this class of endpoint is conventionally deployed
    (protected at the network layer, not the app layer). Safe to leave
    open regardless: every metric here is an aggregate counter/
    histogram over safe labels only (route templates, exception class
    names, status codes) - see app/core/metrics.py's module docstring
    for exactly what is and isn't ever used as a label value."""
    body, content_type = metrics.render_latest()
    return Response(content=body, media_type=content_type)

API_PREFIX = "/api/v1"

app.include_router(health.router, prefix=API_PREFIX)
app.include_router(authentication.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(station.router, prefix=API_PREFIX)
app.include_router(trains.router, prefix=API_PREFIX)
app.include_router(crowd.router, prefix=API_PREFIX)
app.include_router(checkin.router, prefix=API_PREFIX)
app.include_router(checkout.router, prefix=API_PREFIX)
app.include_router(schedule.router, prefix=API_PREFIX)
app.include_router(prediction.router, prefix=API_PREFIX)
app.include_router(analytics.router, prefix=API_PREFIX)
app.include_router(alerts.router, prefix=API_PREFIX)
app.include_router(enquiries.router, prefix=API_PREFIX)
app.include_router(news.router, prefix=API_PREFIX)
app.include_router(notifications.router, prefix=API_PREFIX)
app.include_router(meta.router, prefix=API_PREFIX)
app.include_router(admin.router, prefix=API_PREFIX)
app.include_router(chatbot.router, prefix=API_PREFIX)

@app.get("/")
def home():
    return {
        "message": "MetroFlow Backend Running",
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }

@app.api_route('/healthz', methods=["GET", "HEAD"])
def healthz():
    """Plain liveness probe - deliberately does NOT touch the database
    (that's what /api/v1/health/ is for, as a readiness check). This is
    just "is the process up and serving requests", the convention path
    most container/orchestrator liveness checks (Docker, Kubernetes,
    Render, etc.) look for by default."""
    return {"status": "ok"}

_WS_AUTH_SUBPROTOCOL = "access_token"

@app.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
    """Auth is optional so existing public/anonymous usage (crowd and
    train-position broadcasts) keeps working unauthenticated. When a
    token is present it's decoded with the same Supabase verification
    used on REST requests, and the connection is registered under that
    user_id so notification_service can target them directly (see
    ConnectionManager.notify_user).

    The token travels via the `Sec-WebSocket-Protocol` header, not the
    URL's `?token=` query string: browsers can't set custom headers on
    a WS upgrade request, but the subprotocol list is exactly this
    kind of small out-of-band handshake data, and - unlike a query
    string - it's never written to server access logs, proxy logs,
    Referer headers, or browser history. The client offers two
    subprotocol values, a fixed marker plus the token itself; we read
    both off the header here and, if present, echo the marker back as
    the single accepted subprotocol (required by the handshake spec
    whenever the client sent the header)."""
    offered = [
        p.strip()
        for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
        if p.strip()
    ]
    token = None
    accepted_subprotocol = None
    if len(offered) >= 2 and offered[0] == _WS_AUTH_SUBPROTOCOL:
        accepted_subprotocol = offered[0]
        token = offered[1]

    user = None
    if token:
        db = SessionLocal()
        try:
            user = get_user_from_token_optional(token, db)
        finally:
            db.close()

    await manager.connect(
        websocket,
        user_id=str(user.id) if user else None,
        subprotocol=accepted_subprotocol,
    )
    # Labels the eventual manager.disconnect() call below for
    # ws_disconnects_total (see app/core/metrics.py) - purely
    # observational, doesn't change which exceptions are caught or
    # how they propagate.
    disconnect_reason = "client_close"
    try:
        while True:
            raw = await websocket.receive_text()
            # Any inbound frame is proof this connection is
            # alive - record it so the reaper doesn't treat it as
            # stale (see ConnectionManager.record_activity/_reap_stale).
            manager.record_activity(websocket)
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                message = None
            if isinstance(message, dict) and message.get("type") == "ping":
                # Actually answer the client's own heartbeat
                # ping. Previously this loop only ever called
                # receive_text() and discarded whatever came back, so
                # the frontend's own heartbeat-timeout logic (which
                # treats ANY inbound message as a satisfied heartbeat -
                # see LiveSocketProvider.tsx's onmessage handler) was,
                # by accident, only ever satisfied if an unrelated
                # broadcast happened to land in the same ~10s window.
                # On a quiet page with no crowd/train activity that
                # window could be missed, causing the client to
                # needlessly close and reconnect a perfectly healthy
                # socket.
                try:
                    await websocket.send_text(json.dumps({"event": "pong", "data": {}}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        # Same "any other exception" case the comment below already
        # describes - labeled distinctly from a clean client close so
        # ws_disconnects_total can tell the two apart. Re-raised
        # unchanged: this handler only sets a label, it doesn't
        # swallow anything the code didn't already let through.
        disconnect_reason = "error"
        raise
    finally:
        # Unconditional cleanup. Previously disconnect() was
        # only called inside `except WebSocketDisconnect` - any OTHER
        # exception escaping receive_text()/send_text() (e.g. the
        # transport erroring out on an abrupt close instead of a clean
        # disconnect handshake) skipped cleanup entirely, leaking the
        # connection into active_connections/_connection_users forever
        # (a stale-connection bug in its own right).
        manager.disconnect(websocket, reason=disconnect_reason)
