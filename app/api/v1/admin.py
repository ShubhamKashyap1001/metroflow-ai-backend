from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text

from app.core import cache, log_buffer
from app.core.config import settings
from app.core.rate_limit import ADMIN_LIMIT, limiter
from app.core.security import require_roles
from app.database.database import engine
from app.database.session import SessionLocal
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.admin import SimulatorSettingsUpdate
from app.simulator.scheduler import (
    is_simulator_running,
    is_train_tracker_running,
    pause_simulator,
    pause_train_tracker,
    resume_simulator,
    resume_train_tracker,
    scheduler_status,
)
from app.simulator.control import control_coordination, get_runtime_control, update_runtime_control
from app.websocket.manager import manager

router = APIRouter(prefix="/admin", tags=["Admin"])

@router.get("/simulator")
async def simulator_status(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return {
        "crowd_simulator_running": is_simulator_running(),
        "train_tracker_running": is_train_tracker_running(),
        "control": get_runtime_control(),
        "coordination": control_coordination(),
        "detail": scheduler_status(),
    }

@router.patch("/simulator/settings")
@limiter.limit(ADMIN_LIMIT)
async def simulator_settings(
    request: Request,
    payload: SimulatorSettingsUpdate,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    control = update_runtime_control(**changes)
    return {"control": control, "coordination": control_coordination()}

@router.post("/simulator/start")
@limiter.limit(ADMIN_LIMIT)
async def simulator_start(
    request: Request,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    if not settings.ENABLE_SIMULATOR:
        raise HTTPException(status_code=409, detail="Crowd simulator is disabled by deployment configuration.")
    resume_simulator(SessionLocal, settings.SIMULATOR_INTERVAL_SECONDS)
    return {"crowd_simulator_running": is_simulator_running()}

@router.post("/simulator/stop")
@limiter.limit(ADMIN_LIMIT)
async def simulator_stop(
    request: Request,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    pause_simulator()
    return {"crowd_simulator_running": is_simulator_running()}

@router.post("/train-tracker/start")
@limiter.limit(ADMIN_LIMIT)
async def train_tracker_start(
    request: Request,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    if not settings.ENABLE_TRAIN_TRACKING:
        raise HTTPException(status_code=409, detail="Train tracker is disabled by deployment configuration.")
    resume_train_tracker(SessionLocal, settings.TRAIN_TRACK_INTERVAL_SECONDS)
    return {"train_tracker_running": is_train_tracker_running()}

@router.post("/train-tracker/stop")
@limiter.limit(ADMIN_LIMIT)
async def train_tracker_stop(
    request: Request,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    pause_train_tracker()
    return {"train_tracker_running": is_train_tracker_running()}

@router.get("/logs")
async def get_logs(
    limit: int = 100,
    level: str | None = None,
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return {
        "logs": log_buffer.get_recent_logs(limit=min(max(limit, 1), 500), level=level),
        "counts": log_buffer.log_counts(),
    }

@router.get("/system-status")
async def system_status(
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
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
        "simulator_control": {
            **get_runtime_control(),
            "coordination": control_coordination(),
        },
        "websocket_connections": len(manager.active_connections),
        "log_counts": log_buffer.log_counts(),
        "timestamp": datetime.now(timezone.utc),
    }
