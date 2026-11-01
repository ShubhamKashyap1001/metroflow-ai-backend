import hmac
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.rate_limit import WRITE_LIMIT, limiter
from app.database.session import get_db
from app.enums.crowd_level import CrowdLevel
from app.models.crowd_log import CrowdLog
from app.models.sensor_feed_event import SensorFeedEvent
from app.models.station import Station
from app.models.station_crowd_state import StationCrowdState
from app.models.train import Train
from app.models.train_location import TrainLocation
from app.schemas.sensor_feed import SensorCrowdBatch, SensorCrowdBatchResult, TrainPositionBatch
from app.services.crowd_service import broadcast_crowd_update, invalidate_station_cache, upsert_live_state
from app.services import train_service
from app.websocket.events import TRAIN_POSITION
from app.websocket.manager import manager

router = APIRouter(prefix="/integrations", tags=["External data integrations"])
_SOURCE_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$")

@router.post("/crowd", response_model=SensorCrowdBatchResult)
@limiter.limit(WRITE_LIMIT)
def ingest_crowd_feed(
    request: Request,
    payload: SensorCrowdBatch,
    x_feed_key: str | None = Header(default=None),
    x_feed_source: str = Header(default="sensor"),
    db: Session = Depends(get_db),
):
    configured_key = settings.SENSOR_INGEST_API_KEY
    if not configured_key:
        raise HTTPException(status_code=503, detail="Sensor ingestion is not configured")
    if not x_feed_key or not hmac.compare_digest(x_feed_key, configured_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid feed credentials")
    if not _SOURCE_PATTERN.fullmatch(x_feed_source):
        raise HTTPException(status_code=422, detail="Invalid feed source identifier")

    event_ids = [record.event_id for record in payload.records]
    if len(event_ids) != len(set(event_ids)):
        raise HTTPException(status_code=422, detail="event_id values must be unique within a batch")

    station_codes = {record.station_code.strip() for record in payload.records}
    stations = {
        station.station_code: station
        for station in db.query(Station).filter(Station.station_code.in_(station_codes)).all()
    }
    missing = station_codes.difference(stations)
    if missing:
        raise HTTPException(status_code=422, detail=f"Unknown station_code: {', '.join(sorted(missing))}")

    latest_by_station: dict[int, tuple[Station, int, CrowdLevel, datetime]] = {}
    live_updates: list[tuple[Station, int, CrowdLevel]] = []
    accepted = 0
    duplicates = 0
    for record in sorted(payload.records, key=lambda item: item.observed_at):
        station = stations[record.station_code.strip()]
        event_insert = (
            pg_insert(SensorFeedEvent)
            .values(
                source=x_feed_source,
                event_id=record.event_id,
                station_id=station.id,
                observed_at=record.observed_at,
                observed_count=record.current_count,
            )
            .on_conflict_do_nothing(constraint="uq_sensor_feed_events_source_event")
            .returning(SensorFeedEvent.id)
        )
        inserted_id = db.execute(event_insert).scalar_one_or_none()
        if inserted_id is None:
            duplicates += 1
            continue

        count = record.current_count
        level = CrowdLevel.from_ratio(count / station.capacity if station.capacity else 0)
        db.add(CrowdLog(
            station_id=station.id,
            current_count=count,
            crowd_level=level,
            created_at=record.observed_at,
            updated_at=record.observed_at,
        ))
        accepted += 1
        previous = latest_by_station.get(station.id)
        if previous is None or record.observed_at >= previous[3]:
            latest_by_station[station.id] = (station, count, level, record.observed_at)

    for station_id, (station, count, level, observed_at) in latest_by_station.items():
        live_state = db.get(StationCrowdState, station_id)
        current_timestamp = live_state.updated_at if live_state else None
        if current_timestamp is None or current_timestamp <= observed_at:
            upsert_live_state(db, station_id, count, level, updated_at=observed_at)
            live_updates.append((station, count, level))

    db.commit()
    for station, count, level in live_updates:
        invalidate_station_cache(station)
        broadcast_crowd_update(station, count, level)

    return {"accepted": accepted, "duplicates": duplicates, "rejected": 0}

@router.post("/trains", response_model=SensorCrowdBatchResult)
@limiter.limit(WRITE_LIMIT)
def ingest_train_positions(
    request: Request,
    payload: TrainPositionBatch,
    x_feed_key: str | None = Header(default=None),
    x_feed_source: str = Header(default="metro-operator"),
    db: Session = Depends(get_db),
):
    configured_key = settings.METRO_TRAIN_INGEST_API_KEY
    if not configured_key:
        raise HTTPException(status_code=503, detail="Metro train feed ingestion is not configured")
    if not x_feed_key or not hmac.compare_digest(x_feed_key, configured_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid feed credentials")
    if not _SOURCE_PATTERN.fullmatch(x_feed_source):
        raise HTTPException(status_code=422, detail="Invalid feed source identifier")

    event_ids = [record.event_id for record in payload.records]
    if len(event_ids) != len(set(event_ids)):
        raise HTTPException(status_code=422, detail="event_id values must be unique within a batch")

    station_codes = {
        code
        for record in payload.records
        for code in (record.station_code.strip(), record.next_station_code.strip() if record.next_station_code else None)
        if code
    }
    stations = {
        station.station_code: station
        for station in db.query(Station).filter(Station.station_code.in_(station_codes)).all()
    }
    trains = {
        train.train_number: train
        for train in db.query(Train).filter(Train.train_number.in_({record.train_number.strip() for record in payload.records})).all()
    }
    unknown_stations = station_codes.difference(stations)
    unknown_trains = {record.train_number.strip() for record in payload.records}.difference(trains)
    if unknown_stations or unknown_trains:
        details = []
        if unknown_stations:
            details.append(f"Unknown station_code: {', '.join(sorted(unknown_stations))}")
        if unknown_trains:
            details.append(f"Unknown train_number: {', '.join(sorted(unknown_trains))}")
        raise HTTPException(status_code=422, detail="; ".join(details))

    accepted = 0
    duplicates = 0
    stale = 0
    updates = []
    for record in sorted(payload.records, key=lambda item: item.observed_at):
        train = trains[record.train_number.strip()]
        station = stations[record.station_code.strip()]
        next_station = stations.get(record.next_station_code.strip()) if record.next_station_code else None
        event_insert = (
            pg_insert(SensorFeedEvent)
            .values(
                source=x_feed_source,
                event_id=record.event_id,
                station_id=station.id,
                observed_at=record.observed_at,
            )
            .on_conflict_do_nothing(constraint="uq_sensor_feed_events_source_event")
            .returning(SensorFeedEvent.id)
        )
        if db.execute(event_insert).scalar_one_or_none() is None:
            duplicates += 1
            continue

        location_insert = pg_insert(TrainLocation).values(
            train_id=train.id,
            station_id=station.id,
            next_station_id=next_station.id if next_station else None,
            progress_ratio=record.progress_ratio,
            status=record.status,
            created_at=record.observed_at,
            updated_at=record.observed_at,
        )
        updated_train_id = db.execute(
            location_insert.on_conflict_do_update(
                constraint="uq_train_locations_train_id",
                set_={
                    "station_id": location_insert.excluded.station_id,
                    "next_station_id": location_insert.excluded.next_station_id,
                    "progress_ratio": location_insert.excluded.progress_ratio,
                    "status": location_insert.excluded.status,
                    "updated_at": location_insert.excluded.updated_at,
                },
                where=TrainLocation.updated_at <= location_insert.excluded.updated_at,
            ).returning(TrainLocation.train_id)
        ).scalar_one_or_none()
        accepted += 1
        if updated_train_id is None:
            stale += 1
            continue
        updates.append({
            "train_id": train.id,
            "train_number": train.train_number,
            "from_station_id": station.id,
            "from_station_name": station.station_name,
            "to_station_id": next_station.id if next_station else station.id,
            "to_station_name": next_station.station_name if next_station else station.station_name,
            "progress_ratio": round(record.progress_ratio, 4),
            "delay_minutes": record.delay_minutes,
            "status": "Delayed" if record.delay_minutes else record.status.replace("_", " ").title(),
            "eta_seconds": None,
            "segment_duration_seconds": None,
            "direction": 1,
        })

    if updates:
        db.commit()
        train_service._invalidate_train_position_cache()
        manager.notify(TRAIN_POSITION, {"updates": updates, "timestamp": datetime.now(timezone.utc).isoformat()})
    else:
        db.commit()

    return {"accepted": accepted - stale, "duplicates": duplicates, "rejected": stale}
