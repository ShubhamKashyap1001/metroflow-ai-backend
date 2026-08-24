"""Passenger entry (check-in) - source of the "Passenger Entry & Exit
Records" dataset, and drives live crowd counts."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.journey import CheckInRequest, JourneyResponse
from app.services import journey_service

router = APIRouter(
    prefix="/checkin",
    tags=["Check-In"]
)

@router.post("/", response_model=JourneyResponse, status_code=201)
def check_in(
    payload: CheckInRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return journey_service.check_in(
        db,
        user_id=str(current_user.id),
        source_station_id=payload.source_station_id,
        destination_station_id=payload.destination_station_id,
    )
