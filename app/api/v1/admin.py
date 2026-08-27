from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core import cache, log_buffer
from app.core.config import settings
from app.core.security import require_roles
from app.database.database import engine
from app.database.session import SessionLocal
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.simulator.scheduler import (
    is_simulator_running,
    is_train_tracker_running,
    scheduler_status,
    start_simulator,
    start_train_tracker,
    stop_simulator,
    stop_train_tracker,
)
from app.websocket.manager import manager

router = APIRouter(prefix="/admin", tags=["Admin"])

@router.get("/simulator")
async def simulator_status(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return {
                                                                   
        "crowd_simulator_running": is_simulator_running(),
        "train_tracker_running": is_train_tracker_running(),
                                                                          
        "detail": scheduler_status(),
    }

@router.post("/simulator/start")
async def simulator_start(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    start_simulator(SessionLocal, settings.SIMULATOR_INTERVAL_SECONDS)
    return {"crowd_simulator_running": is_simulator_running()}

@router.post("/simulator/stop")
async def simulator_stop(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    await stop_simulator()
    return {"crowd_simulator_running": is_simulator_running()}

@router.post("/train-tracker/start")
async def train_tracker_start(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    start_train_tracker(SessionLocal, settings.TRAIN_TRACK_INTERVAL_SECONDS)
    return {"train_tracker_running": is_train_tracker_running()}

@router.post("/train-tracker/stop")
async def train_tracker_stop(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    await stop_train_tracker()
    return {"train_tracker_running": is_train_tracker_running()}

@router.get("/logs")
async def get_logs(
    limit: int = 100,
    level: str | None = None,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    """Recent real application log records (in-process ring buffer -
    see app/core/log_buffer.py). Not synthetic - this is exactly what
    the running server has logged, newest first."""
    return {
        "logs": log_buffer.get_recent_logs(limit=min(max(limit, 1), 500), level=level),
        "counts": log_buffer.log_counts(),
    }

@router.get("/system-status")
async def system_status(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    """One combined snapshot for a System Status screen: DB, cache,
    background workers, and live socket connections - all read live
    from the running process, not cached/mocked."""
    db_ok = True
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return {
        "app_name": settings.APP_NAME,
        "app_version": settings.APP_VERSION,
        "database": {"connected": db_ok},
        "redis": cache.redis_status(),
        "crowd_simulator_running": is_simulator_running(),
        "train_tracker_running": is_train_tracker_running(),
        "scheduler": scheduler_status(),
        "websocket_connections": len(manager.active_connections),
        "log_counts": log_buffer.log_counts(),
        "timestamp": datetime.utcnow(),
    }
