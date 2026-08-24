
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.crowd_log import CrowdLog
from app.models.line_station import LineStation
from app.models.metro_line import MetroLine
from app.models.prediction import Prediction
from app.models.station import Station
from app.models.train_schedule import TrainSchedule
from app.utils.geo import cities_for_state

def traffic_analysis_report(db: Session, hours: int = 24, state: str | None = None) -> dict:
    since = datetime.utcnow() - timedelta(hours=hours)
    cities = cities_for_state(state)

    per_station_query = (
        db.query(
            CrowdLog.station_id,
            func.avg(CrowdLog.current_count).label("avg_count"),
            func.max(CrowdLog.current_count).label("peak_count"),
        )
        .filter(CrowdLog.created_at >= since)
    )
    if cities:
        per_station_query = per_station_query.join(
            Station, Station.id == CrowdLog.station_id
        ).filter(Station.city.in_(cities))
    per_station = per_station_query.group_by(CrowdLog.station_id).all()

    station_query = db.query(Station.id, Station.station_name)
    if cities:
        station_query = station_query.filter(Station.city.in_(cities))
    stations = {sid: name for sid, name in station_query.all()}

    report_rows = [
        {
            "station_id": row.station_id,
            "station_name": stations.get(row.station_id, "Unknown"),
            "average_count": round(row.avg_count, 1) if row.avg_count else 0,
            "peak_count": row.peak_count or 0,
        }
        for row in per_station
    ]

    busiest = max(report_rows, key=lambda r: r["peak_count"], default=None)
    delayed_query = db.query(TrainSchedule).filter(TrainSchedule.delay_minutes > 0)
    if cities:
        delayed_query = delayed_query.join(
            Station, Station.id == TrainSchedule.station_id
        ).filter(Station.city.in_(cities))
    total_delayed = delayed_query.count()

    return {
        "window_hours": hours,
        "stations": sorted(report_rows, key=lambda r: r["peak_count"], reverse=True),
        "busiest_station": busiest,
        "currently_delayed_schedules": total_delayed,
        "generated_at": datetime.utcnow(),
    }

def prediction_insights(db: Session, limit: int = 20, state: str | None = None) -> list[Prediction]:
    query = db.query(Prediction)
    cities = cities_for_state(state)
    if cities:
        query = query.join(Station, Station.id == Prediction.station_id).filter(
            Station.city.in_(cities)
        )
    return query.order_by(Prediction.created_at.desc()).limit(limit).all()

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
        "generated_at": datetime.utcnow(),
    }

def passenger_flow_overview(
    db: Session,
    hours: float = 24,
    state: str | None = None,
    top_n: int = 8,
) -> dict:
    """Powers the "Passenger Flow by Station" chart, the four KPI cards,
    and the "Ridership by Line" donut on the Analytics page.

    Entries/exits are derived the same way crowd_service.get_inflow_outflow
    already derives them for a single station - consecutive CrowdLog
    samples rising = passengers entering, falling = passengers exiting -
    just run for every station in `state` at once instead of one at a
    time, and summed/grouped for the KPI totals, per-station chart rows,
    and per-line ridership breakdown.

    `hours` is deliberately a short rolling window by default at the call
    site (0.5 = last 30 minutes), not a 24h cumulative one: with a 24h
    cumulative denominator, one more 5-second simulator tick barely moves
    the total at all, so the KPI cards and chart looked frozen even
    though fresh data was arriving continuously. A short window makes
    each new sample a visible fraction of the total instead.
    """
    since = datetime.utcnow() - timedelta(hours=hours)
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

    logs = (
        db.query(CrowdLog.station_id, CrowdLog.current_count, CrowdLog.created_at)
        .filter(CrowdLog.station_id.in_(station_ids), CrowdLog.created_at >= since)
        .order_by(CrowdLog.station_id, CrowdLog.created_at.asc())
        .all()
    )

    entries_by_station: dict[int, int] = defaultdict(int)
    exits_by_station: dict[int, int] = defaultdict(int)
    last_count_by_station: dict[int, int] = {}
    previous_count: dict[int, int] = {}

    for log in logs:
        prev = previous_count.get(log.station_id)
        if prev is not None:
            delta = log.current_count - prev
            if delta > 0:
                entries_by_station[log.station_id] += delta
            elif delta < 0:
                exits_by_station[log.station_id] += abs(delta)
        previous_count[log.station_id] = log.current_count
        last_count_by_station[log.station_id] = log.current_count

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
        key=lambda row: row["entries"],
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
        "generated_at": datetime.utcnow(),
    }
