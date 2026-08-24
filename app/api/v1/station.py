"""Milestone 1 - Station master data (metro rail systems, smart city
transportation setup). Backs the Crowd Monitoring & Scheduling modules,
which reference stations by id.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_roles
from app.database.session import get_db
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.station import StationCreate, StationResponse, StationUpdate
from app.services import station_service

router = APIRouter(
    prefix="/stations",
    tags=["Stations"]
)

@router.get("/", response_model=list[StationResponse])
def list_stations(city: str | None = None, state: str | None = None, db: Session = Depends(get_db)):
    return station_service.list_stations(db, city, state)

@router.get("/{station_id}", response_model=StationResponse)
def get_station(station_id: int, db: Session = Depends(get_db)):
    return station_service.get_station(db, station_id)

@router.post("/", response_model=StationResponse, status_code=201)
def create_station(
    payload: StationCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return station_service.create_station(db, payload)

@router.put("/{station_id}", response_model=StationResponse)
def update_station(
    station_id: int,
    payload: StationUpdate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return station_service.update_station(db, station_id, payload)

@router.delete("/{station_id}", status_code=204)
def delete_station(
    station_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN)),
):
    station_service.delete_station(db, station_id)
