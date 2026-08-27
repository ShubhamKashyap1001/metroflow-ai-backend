"""Milestone 1 - Crowd Monitoring Module.

Fixed: previously imported a non-existent `database.supabase` client and
had no request validation. Now backed by SQLAlchemy + crowd_service.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.enums.crowd_level import CrowdLevel
from app.schemas.crowd_log import CrowdLogCreate, CrowdLogResponse
from app.services import crowd_service

router = APIRouter(
    prefix="/crowd",
    tags=["Crowd Management"]
)

@router.post("/", response_model=CrowdLogResponse, status_code=201)
def log_crowd(payload: CrowdLogCreate, db: Session = Depends(get_db)):
    """Ingest a passenger density reading for a station (ticketing / sensor feed)."""
    return crowd_service.log_crowd_count(db, payload)

@router.get("/dashboard")
def crowd_dashboard(state: str | None = None, db: Session = Depends(get_db)):
    """Live station-wise crowd snapshot for the monitoring dashboard."""
    return crowd_service.get_station_wise_snapshot(db, state)

@router.get("/heatmap")
def crowd_heatmap(
    state: str | None = None,
    limit: int | None = None,
    db: Session = Depends(get_db),
):
    """Crowd heatmap generation (station coordinates + density).

    Pass ?limit=20 to get only the 20 busiest stations (by current
    occupancy) instead of every station - used by the dashboard's
    "Top 20" filter toggle.
    """
    return crowd_service.get_heatmap(db, state, limit)

@router.get("/congestion")
def congestion(
    min_level: CrowdLevel = CrowdLevel.HIGH,
    state: str | None = None,
    db: Session = Depends(get_db),
):
    """Congestion monitoring: stations at/above a given crowd level."""
    return crowd_service.get_congested_stations(db, min_level, state)

@router.get("/station-monitor")
def station_monitor(
    state: str | None = None,
    hours: int = 1,
    db: Session = Depends(get_db),
):
    """Live Station Monitor feed for the dashboard: every active
    station's density + a short-window passenger in/out delta,
    busiest first. `state` scopes it to one city/state - pass the
    currently selected city so the widget only shows that city's
    stations."""
    return crowd_service.get_station_monitor(db, state, hours)


@router.get("/{station_id}")
def get_station_crowd(station_id: int, db: Session = Depends(get_db)):
    latest = crowd_service.get_latest_crowd(db, station_id)
    if not latest:
        return {"message": "No crowd data recorded for this station yet"}
    return CrowdLogResponse.model_validate(latest)

@router.get("/{station_id}/inflow-outflow")
def inflow_outflow(station_id: int, hours: int = 24, db: Session = Depends(get_db)):
    """Passenger inflow and outflow analysis."""
    return crowd_service.get_inflow_outflow(db, station_id, hours)

@router.get("/{station_id}/analytics")
def station_analytics(station_id: int, db: Session = Depends(get_db)):
    """Station-wise analytics."""
    return crowd_service.get_station_analytics(db, station_id)
