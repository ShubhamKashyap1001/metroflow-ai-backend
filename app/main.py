import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_ipaddr
from sqlalchemy.exc import TimeoutError as SATimeoutError

logger = logging.getLogger(__name__)

from app.api.v1 import (
    admin,
    alerts,
    analytics,
    authentication,
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
from app.core.config import settings
from app.database.session import SessionLocal
from app.enums.notification_source import NotificationSource
from app.services import notification_service
from app.simulator.scheduler import (
    start_simulator,
    start_train_tracker,
    stop_simulator,
    stop_train_tracker,
)
from app.websocket.manager import manager

limiter = Limiter(key_func=get_ipaddr, default_limits=["100/minute"])

@asynccontextmanager
async def lifespan(app: FastAPI):
    manager.bind_loop(asyncio.get_running_loop())

    tick = settings.SIMULATOR_INTERVAL_SECONDS
    if settings.ENABLE_SIMULATOR:
        start_simulator(SessionLocal, tick)
    if settings.ENABLE_TRAIN_TRACKING:
        start_train_tracker(SessionLocal, tick)
    yield
    if settings.ENABLE_SIMULATOR:
        await stop_simulator()
    if settings.ENABLE_TRAIN_TRACKING:
        await stop_train_tracker()

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[] if settings.cors_origins_list == ["*"] else settings.cors_origins_list,
    allow_origin_regex=".*" if settings.cors_origins_list == ["*"] else None,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

@app.get("/")
def home():
    return {
        "message": "MetroFlow Backend Running",
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }

@app.get("/healthz")
def healthz():
    """Plain liveness probe - deliberately does NOT touch the database
    (that's what /api/v1/health/ is for, as a readiness check). This is
    just "is the process up and serving requests", the convention path
    most container/orchestrator liveness checks (Docker, Kubernetes,
    Render, etc.) look for by default."""
    return {"status": "ok"}

@app.websocket("/ws/monitor")
async def websocket_monitor(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
