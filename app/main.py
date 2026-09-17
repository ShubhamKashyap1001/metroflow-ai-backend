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
from app.core.request_limits import MaxBodySizeMiddleware
from sqlalchemy.exc import TimeoutError as SATimeoutError

logger = logging.getLogger(__name__)

from app.core import log_buffer, metrics
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
    saved_routes,
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
from app.services import notification_dispatch_queue
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
    manager.start_relay()
    manager.start_reaper()

    if settings.AI_EAGER_WARMUP:
        from app.ai_engine.warmup import warm_up_models
        await asyncio.to_thread(warm_up_models)

    resumed = await asyncio.to_thread(notification_dispatch_queue.recover_pending_jobs)
    if resumed:
        logger.warning(
            "Resumed %d notification dispatch job(s) left over from a previous process.",
            resumed,
        )

    if settings.ENABLE_SIMULATOR:
        start_simulator(SessionLocal, settings.SIMULATOR_INTERVAL_SECONDS)
    if settings.ENABLE_TRAIN_TRACKING:
        start_train_tracker(SessionLocal, settings.TRAIN_TRACK_INTERVAL_SECONDS)
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
    notification_executor.shutdown(wait=True)

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler_with_retry_after)
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(SATimeoutError)
async def db_pool_exhausted_handler(request: Request, exc: SATimeoutError):
    logger.error("[db] connection pool exhausted on %s - consider raising DB_POOL_SIZE/DB_MAX_OVERFLOW "
                  "or checking for a slow query/unreachable DB.", request.url.path)
    metrics.record_db_failure("pool_exhausted")

    # Deliberately NOT writing a "pool exhausted" notification to the DB
    # here. Opening `SessionLocal()` to record this would try to check
    # out yet another connection from the exact pool that has none free
    # - that checkout attempt blocks for the full DB_POOL_TIMEOUT again
    # (turning what should be an immediate 503 into one that hangs for
    # ~DB_POOL_TIMEOUT seconds first), and under a burst of these errors
    # every handler instance competing for the same scarce connections
    # makes the exhaustion worse instead of just reporting it. The
    # logger.error + metrics counter above are DB-free and already
    # sufficient for alerting on this condition.

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

app.add_middleware(MaxBodySizeMiddleware, max_body_size=settings.MAX_REQUEST_BODY_BYTES)

@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
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
app.include_router(saved_routes.router, prefix=API_PREFIX)
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
    return {"status": "ok"}

_WS_AUTH_SUBPROTOCOL = "access_token"

@app.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
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

    accepted = await manager.connect(
        websocket,
        user_id=str(user.id) if user else None,
        subprotocol=accepted_subprotocol,
    )
    if not accepted:
        return
    disconnect_reason = "client_close"
    try:
        while True:
            raw = await websocket.receive_text()
            manager.record_activity(websocket)
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                message = None
            if isinstance(message, dict) and message.get("type") == "ping":
                try:
                    await websocket.send_text(json.dumps({"event": "pong", "data": {}}))
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        disconnect_reason = "error"
        raise
    finally:
        manager.disconnect(websocket, reason=disconnect_reason)