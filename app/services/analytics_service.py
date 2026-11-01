
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.models.crowd_log import CrowdLog
from app.models.crowd_log_hourly import CrowdLogHourly
from app.models.alert import Alert
from app.models.line_station import LineStation
from app.models.metro_line import MetroLine
from app.models.prediction import Prediction
from app.models.station import Station
from app.models.train_schedule import TrainSchedule
from app.enums.prediction_type import PredictionType
from app.core.config import settings
from app.services.crowd_service import crowd_flow_aggregate
from app.utils.geo import cities_for_state

MAX_PREDICTION_INSIGHTS_LIMIT = 200

MAX_HISTORY_WINDOW_HOURS = 720  

def _clamp_hours(hours: float) -> float:
    return min(max(hours or 1, 1), MAX_HISTORY_WINDOW_HOURS)

def _traffic_aggregates_for_period(
    db: Session,
    since: datetime,
    until: datetime,
    cities: list[str] | None,
    station_id: int | None,
    line_name: str | None,
) -> dict[int, dict]:
    def apply_filters(query, station_column):
        query = query.join(Station, Station.id == station_column)
        if cities:
            query = query.filter(Station.city.in_(cities))
        if station_id is not None:
            query = query.filter(station_column == station_id)
        if line_name:
            query = query.join(LineStation, LineStation.station_id == station_column).join(
                MetroLine, MetroLine.id == LineStation.line_id
            ).filter(MetroLine.line_name == line_name, MetroLine.is_active.is_(True))
        return query

    aggregates: dict[int, dict] = {}
    raw = db.query(
        CrowdLog.station_id,
        Station.station_name,
        func.avg(CrowdLog.current_count).label("average_count"),
        func.max(CrowdLog.current_count).label("peak_count"),
        func.count(CrowdLog.id).label("sample_count"),
    ).filter(CrowdLog.created_at >= since, CrowdLog.created_at < until)
    raw = apply_filters(raw, CrowdLog.station_id)
    for sid, name, average, peak, count in raw.group_by(CrowdLog.station_id, Station.station_name).all():
        aggregates[sid] = {
            "station_name": name,
            "weighted_total": float(average or 0) * count,
            "sample_count": count,
            "peak_count": peak or 0,
        }

    hourly = db.query(
        CrowdLogHourly.station_id,
        Station.station_name,
        func.sum(CrowdLogHourly.avg_count * CrowdLogHourly.sample_count).label("weighted_total"),
        func.max(CrowdLogHourly.max_count).label("peak_count"),
        func.sum(CrowdLogHourly.sample_count).label("sample_count"),
    ).filter(CrowdLogHourly.hour_bucket >= since, CrowdLogHourly.hour_bucket < until)
    hourly = apply_filters(hourly, CrowdLogHourly.station_id)
    for sid, name, weighted_total, peak, count in hourly.group_by(
        CrowdLogHourly.station_id, Station.station_name
    ).all():
        row = aggregates.setdefault(sid, {
            "station_name": name,
            "weighted_total": 0.0,
            "sample_count": 0,
            "peak_count": 0,
        })
        row["weighted_total"] += float(weighted_total or 0)
        row["sample_count"] += int(count or 0)
        row["peak_count"] = max(row["peak_count"], peak or 0)

    for values in aggregates.values():
        values["average_count"] = (
            round(values["weighted_total"] / values["sample_count"], 1)
            if values["sample_count"] else 0.0
        )
    return aggregates

def data_freshness_report(db: Session, state: str | None = None) -> dict:
    observed_at = datetime.now(timezone.utc)
    stale_after = max(settings.CROWD_DATA_STALE_AFTER_SECONDS, 30)
    cities = cities_for_state(state)
    station_query = db.query(Station.id, Station.station_name, Station.city).filter(
        Station.is_active.is_(True)
    )
    if cities:
        station_query = station_query.filter(Station.city.in_(cities))
    stations = station_query.order_by(Station.station_name).all()
    station_ids = [row.id for row in stations]
    latest_by_station: dict[int, datetime] = {}
    if station_ids:

        for model, column in (
            (CrowdLog, CrowdLog.created_at),
            (CrowdLogHourly, CrowdLogHourly.hour_bucket),
        ):
            rows = (
                db.query(model.station_id, func.max(column).label("latest_at"))
                .filter(model.station_id.in_(station_ids))
                .group_by(model.station_id)
                .all()
            )
            for station_id, latest in rows:
                if latest is None:
                    continue
                if latest.tzinfo is None:
                    latest = latest.replace(tzinfo=timezone.utc)
                if station_id not in latest_by_station or latest > latest_by_station[station_id]:
                    latest_by_station[station_id] = latest

    line_by_station: dict[int, str] = {}
    if station_ids:
        links = (
            db.query(LineStation.station_id, MetroLine.line_name)
            .join(MetroLine, MetroLine.id == LineStation.line_id)
            .filter(LineStation.station_id.in_(station_ids), MetroLine.is_active.is_(True))
            .order_by(MetroLine.line_name)
            .all()
        )
        for station_id, line_name in links:
            line_by_station.setdefault(station_id, line_name)

    output = []
    for station in stations:
        latest = latest_by_station.get(station.id)
        age_seconds = max(int((observed_at - latest).total_seconds()), 0) if latest else None
        status = "missing" if latest is None else "stale" if age_seconds > stale_after else "fresh"
        output.append({
            "station_id": station.id,
            "station_name": station.station_name,
            "city": station.city,
            "line_name": line_by_station.get(station.id),
            "latest_crowd_log_at": latest,
            "age_seconds": age_seconds,
            "status": status,
        })
    return {
        "observed_at": observed_at,
        "stale_after_seconds": stale_after,
        "total_stations": len(output),
        "fresh_stations": sum(row["status"] == "fresh" for row in output),
        "stale_stations": sum(row["status"] == "stale" for row in output),
        "missing_stations": sum(row["status"] == "missing" for row in output),
        "stations": output,
    }

def traffic_comparison_report(
    db: Session,
    current_start: date,
    current_end: date,
    previous_start: date,
    previous_end: date,
    state: str | None = None,
    station_id: int | None = None,
    line_name: str | None = None,
) -> dict:
    if current_start > current_end or previous_start > previous_end:
        raise HTTPException(status_code=422, detail="Each start date must be on or before its end date.")
    current_days = (current_end - current_start).days
    previous_days = (previous_end - previous_start).days
    if current_days != previous_days or current_days >= 30:
        raise HTTPException(status_code=422, detail="Comparison windows must have the same length and be at most 30 days.")
    if current_end >= previous_start and previous_end >= current_start:
        raise HTTPException(status_code=422, detail="Comparison date ranges cannot overlap.")

    cities = cities_for_state(state)
    current = _traffic_aggregates_for_period(
        db,
        datetime.combine(current_start, time.min, tzinfo=timezone.utc),
        datetime.combine(current_end + timedelta(days=1), time.min, tzinfo=timezone.utc),
        cities,
        station_id,
        line_name,
    )
    previous = _traffic_aggregates_for_period(
        db,
        datetime.combine(previous_start, time.min, tzinfo=timezone.utc),
        datetime.combine(previous_end + timedelta(days=1), time.min, tzinfo=timezone.utc),
        cities,
        station_id,
        line_name,
    )
    station_ids = sorted(current.keys() | previous.keys())
    rows = []
    for sid in station_ids:
        current_row = current.get(sid)
        previous_row = previous.get(sid)
        current_average = current_row["average_count"] if current_row else 0.0
        previous_average = previous_row["average_count"] if previous_row else 0.0
        change = (
            round(((current_average - previous_average) / previous_average) * 100, 1)
            if previous_average else None
        )
        rows.append({
            "station_id": sid,
            "station_name": (current_row or previous_row)["station_name"],
            "current_average_count": current_average,
            "current_peak_count": current_row["peak_count"] if current_row else 0,
            "previous_average_count": previous_average,
            "previous_peak_count": previous_row["peak_count"] if previous_row else 0,
            "average_change_percent": change,
        })
    rows.sort(key=lambda row: row["current_average_count"], reverse=True)
    return {
        "current_start_date": current_start.isoformat(),
        "current_end_date": current_end.isoformat(),
        "previous_start_date": previous_start.isoformat(),
        "previous_end_date": previous_end.isoformat(),
        "station_id": station_id,
        "line_name": line_name,
        "state": state,
        "stations": rows,
        "generated_at": datetime.now(timezone.utc),
    }

def traffic_analysis_report(
    db: Session,
    hours: int = 24,
    state: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    station_id: int | None = None,
    line_name: str | None = None,
) -> dict:
    if (start_date is None) != (end_date is None):
        raise HTTPException(status_code=422, detail="Both start_date and end_date are required.")
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="Start date must be on or before end date.")
    if start_date and end_date and (end_date - start_date).days >= 30:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 30 days.")

    if start_date and end_date:
        since = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
        until = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=timezone.utc)
        window_hours = int((until - since).total_seconds() // 3600)
    else:
        hours = _clamp_hours(hours)
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        until = None
        window_hours = hours
    cities = cities_for_state(state)

    per_station = _traffic_aggregates_for_period(
        db, since, until or datetime.now(timezone.utc), cities, station_id, line_name
    )

    report_rows = [
        {
            "station_id": station_id_row,
            "station_name": values["station_name"],
            "average_count": values["average_count"],
            "peak_count": values["peak_count"],
        }
        for station_id_row, values in per_station.items()
    ]

    busiest = max(report_rows, key=lambda r: r["peak_count"], default=None)
    delayed_query = db.query(TrainSchedule).filter(TrainSchedule.delay_minutes > 0)
    if cities:
        delayed_query = delayed_query.join(
            Station, Station.id == TrainSchedule.station_id
        ).filter(Station.city.in_(cities))
    total_delayed = delayed_query.count()

    return {
        "window_hours": window_hours,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "station_id": station_id,
        "line_name": line_name,
        "state": state,
        "stations": sorted(report_rows, key=lambda r: r["peak_count"], reverse=True),
        "busiest_station": busiest,
        "currently_delayed_schedules": total_delayed,
        "generated_at": datetime.now(timezone.utc),
    }

def prediction_insights(db: Session, limit: int = 20, state: str | None = None) -> list[Prediction]:
    limit = min(max(limit or 20, 1), MAX_PREDICTION_INSIGHTS_LIMIT)
    query = db.query(Prediction)
    cities = cities_for_state(state)
    if cities:
        query = query.join(Station, Station.id == Prediction.station_id).filter(
            Station.city.in_(cities)
        )
    return query.order_by(Prediction.created_at.desc()).limit(limit).all()

def passenger_travel_updates(db: Session, state: str | None = None) -> dict:
    cities = cities_for_state(state)
    prediction_query = (
        db.query(Prediction)
        .join(Station, Station.id == Prediction.station_id)
        .filter(
            Prediction.prediction_type.in_(
                [PredictionType.CROWD, PredictionType.DEMAND, PredictionType.DELAY]
            )
        )
    )
    alert_query = (
        db.query(Alert)
        .join(Station, Station.id == Alert.station_id)
        .filter(
            Alert.is_resolved.is_(False),
            or_(Alert.available_until.is_(None), Alert.available_until >= datetime.now(timezone.utc)),
        )
    )
    if cities:
        prediction_query = prediction_query.filter(Station.city.in_(cities))
        alert_query = alert_query.filter(Station.city.in_(cities))

    predictions = prediction_query.order_by(Prediction.created_at.desc()).limit(15).all()
    advisories = alert_query.order_by(Alert.created_at.desc()).limit(15).all()

    return {
        "predictions": [
            {
                "id": item.id,
                "station_id": item.station_id,
                "prediction_type": item.prediction_type.value,
                "predicted_value": item.predicted_value,
                "target_datetime": item.target_datetime,
                "created_at": item.created_at,
            }
            for item in predictions
        ],
        "advisories": [
            {
                "id": item.id,
                "station_id": item.station_id,
                "alert_type": item.alert_type.value,
                "message": item.message,
                "available_until": item.available_until,
                "created_at": item.created_at,
            }
            for item in advisories
        ],
    }

def operational_monitoring_summary(db: Session, state: str | None = None) -> dict:
    cities = cities_for_state(state)

    station_query = db.query(Station).filter(Station.is_active.is_(True))
    if cities:
        station_query = station_query.filter(Station.city.in_(cities))
    total_stations = station_query.count()

    schedule_stats_query = db.query(
        func.count(TrainSchedule.id).label("total"),
        func.count(TrainSchedule.id).filter(TrainSchedule.delay_minutes > 0).label("delayed"),
    )
    if cities:
        schedule_stats_query = schedule_stats_query.join(
            Station, Station.id == TrainSchedule.station_id
        ).filter(Station.city.in_(cities))
    total_schedules, delayed = schedule_stats_query.one()
    total_schedules = total_schedules or 0
    delayed = delayed or 0

    return {
        "active_stations": total_stations,
        "total_scheduled_trips": total_schedules,
        "currently_delayed": delayed,
        "on_time_rate": round(1 - (delayed / total_schedules), 3) if total_schedules else 1.0,
    }

def _empty_passenger_flow_overview(hours: float) -> dict:
    return {
        "window_hours": hours,
        "total_inflow": 0,
        "total_outflow": 0,
        "net_flow": 0,
        "avg_predicted_occupancy": 0.0,
        "top_stations": [],
        "ridership_by_line": [],
        "generated_at": datetime.now(timezone.utc),
    }

def passenger_flow_overview(
    db: Session,
    hours: float = 24,
    state: str | None = None,
    top_n: int = 8,
) -> dict:
    hours = _clamp_hours(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    cities = cities_for_state(state)

    station_query = db.query(Station.id, Station.station_name, Station.capacity).filter(
        Station.is_active.is_(True)
    )
    if cities:
        station_query = station_query.filter(Station.city.in_(cities))
    stations = station_query.all()

    if not stations:
        return _empty_passenger_flow_overview(hours)

    station_ids = [s.id for s in stations]

    flow = crowd_flow_aggregate(db, station_ids, since)
    entries_by_station: dict[int, int] = {
        sid: values["inflow"] for sid, values in flow.items() if values["inflow"]
    }
    exits_by_station: dict[int, int] = {
        sid: values["outflow"] for sid, values in flow.items() if values["outflow"]
    }
    last_count_by_station: dict[int, int] = {
        sid: values["last_count"]
        for sid, values in flow.items()
        if values["last_count"] is not None
    }

    total_inflow = sum(entries_by_station.values())
    total_outflow = sum(exits_by_station.values())

    occupancy_ratios = []
    for station in stations:
        count = last_count_by_station.get(station.id)
        if count is not None and station.capacity:
            occupancy_ratios.append(min(count / station.capacity, 1))
    avg_predicted_occupancy = (
        round(sum(occupancy_ratios) / len(occupancy_ratios), 4) if occupancy_ratios else 0.0
    )

    top_stations = sorted(
        (
            {
                "station_id": station.id,
                "station_name": station.station_name,
                "entries": entries_by_station.get(station.id, 0),
                "exits": exits_by_station.get(station.id, 0),
            }
            for station in stations
        ),
        key=lambda row: row["entries"] + row["exits"],
        reverse=True,
    )[:top_n]

    line_query = (
        db.query(MetroLine.line_name, MetroLine.color, LineStation.station_id)
        .join(LineStation, LineStation.line_id == MetroLine.id)
        .filter(MetroLine.is_active.is_(True), LineStation.station_id.in_(station_ids))
    )
    ridership_by_line_map: dict[str, dict] = {}
    for line_name, color, station_id in line_query.all():
        bucket = ridership_by_line_map.setdefault(
            line_name, {"line_name": line_name, "color": color, "passenger_count": 0}
        )
        bucket["passenger_count"] += entries_by_station.get(station_id, 0)

    ridership_by_line = sorted(
        ridership_by_line_map.values(), key=lambda row: row["passenger_count"], reverse=True
    )

    return {
        "window_hours": hours,
        "total_inflow": total_inflow,
        "total_outflow": total_outflow,
        "net_flow": total_inflow - total_outflow,
        "avg_predicted_occupancy": avg_predicted_occupancy,
        "top_stations": top_stations,
        "ridership_by_line": ridership_by_line,
        "generated_at": datetime.now(timezone.utc),
    }
