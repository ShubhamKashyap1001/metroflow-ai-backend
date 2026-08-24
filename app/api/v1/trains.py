from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import require_roles
from app.database.session import get_db
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.train import (
    TrainCreate,
    TrainLiveResponse,
    TrainResponse,
    TrainRouteResponse,
    TrainUpdate,
)
from app.services import train_service

router = APIRouter(
    prefix="/trains",
    tags=["Trains"]
)

@router.get("/", response_model=list[TrainResponse])
def get_trains(state: str | None = None, db: Session = Depends(get_db)):
    return train_service.list_trains(db, state)

@router.get("/live", response_model=list[TrainLiveResponse])
def get_live_train_positions(state: str | None = None, db: Session = Depends(get_db)):
    return train_service.list_live_positions(db, state)

@router.get("/routes", response_model=list[TrainRouteResponse])
def get_train_routes(state: str | None = None, db: Session = Depends(get_db)):
    return train_service.list_routes(db, state)

@router.get("/{train_id}", response_model=TrainResponse)
def get_train(train_id: int, db: Session = Depends(get_db)):
    return train_service.get_train(db, train_id)

@router.post("/", response_model=TrainResponse, status_code=201)
def create_train(
    payload: TrainCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return train_service.create_train(db, payload)

@router.put("/{train_id}", response_model=TrainResponse)
def update_train(
    train_id: int,
    payload: TrainUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return train_service.update_train(db, train_id, payload)
