"""Passenger exit (check-out) - closes the journey, computes fare,
updates crowd counts at source/destination."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.journey import CheckOutRequest, JourneyResponse
from app.services import journey_service

router = APIRouter(
    prefix="/checkout",
    tags=["Check-Out"]
)

@router.post("/", response_model=JourneyResponse)
@limiter.limit(WRITE_LIMIT)
def check_out(
    request: Request,
    payload: CheckOutRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return journey_service.check_out(
        db,
        user_id=str(current_user.id),
        journey_id=payload.journey_id,
    )

@router.get("/active", response_model=JourneyResponse | None)
def active_journey(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return journey_service.active_journey_for_user(db, str(current_user.id))
