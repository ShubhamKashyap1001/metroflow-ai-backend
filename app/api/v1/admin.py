from fastapi import APIRouter, Depends

from app.core.config import settings
from app.core.security import require_roles
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
