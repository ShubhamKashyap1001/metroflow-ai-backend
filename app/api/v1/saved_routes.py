
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import WRITE_LIMIT, limiter
from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.saved_route import (
    SavedRouteCreate,
    SavedRouteLiveStatus,
    SavedRouteResponse,
)
from app.services import saved_route_service

router = APIRouter(
    prefix="/saved-routes",
    tags=["Saved Routes"],
)


def _to_response(route) -> SavedRouteResponse:
    return SavedRouteResponse(
        id=route.id,
        origin_station_id=route.origin_station_id,
        destination_station_id=route.destination_station_id,
        origin_station_name=route.origin_station.station_name if route.origin_station else "",
        destination_station_name=route.destination_station.station_name if route.destination_station else "",
    )


@router.get("/me", response_model=SavedRouteResponse | None)
def get_my_saved_route(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    route = saved_route_service.get_my_route(db, str(current_user.id))
    return _to_response(route) if route else None


@router.put("/me", response_model=SavedRouteResponse)
@limiter.limit(WRITE_LIMIT)
def set_my_saved_route(
    request: Request,
    payload: SavedRouteCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    route = saved_route_service.set_my_route(
        db,
        str(current_user.id),
        payload.origin_station_id,
        payload.destination_station_id,
    )
    return _to_response(route)


@router.delete("/me", status_code=204)
@limiter.limit(WRITE_LIMIT)
def delete_my_saved_route(
    request: Request,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    saved_route_service.delete_my_route(db, str(current_user.id))


@router.get("/me/live", response_model=SavedRouteLiveStatus)
def get_my_saved_route_live(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    result = saved_route_service.get_my_route_live(db, str(current_user.id))
    if result is None:
        raise HTTPException(status_code=404, detail="No saved route yet.")
    return result