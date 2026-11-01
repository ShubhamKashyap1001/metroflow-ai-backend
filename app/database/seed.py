from datetime import time

from app.database.init_db import create_tables
from app.database.session import SessionLocal
from app.enums.day_type import DayType
from app.models.crowd_log import CrowdLog
from app.models.line_station import LineStation
from app.models.metro_line import MetroLine
from app.models.station import Station
from app.models.train import Train
from app.models.train_schedule import TrainSchedule
from app.simulator.station_generator import DEMO_LINE, DEMO_STATIONS, DEMO_TRAINS

def seed() -> None:
    create_tables()
    db = SessionLocal()

    try:
        if db.query(Station).count() > 0:
            print("Database already seeded, skipping.")
            return

        stations = [Station(**data) for data in DEMO_STATIONS]
        db.add_all(stations)
        db.flush()                              

        line = MetroLine(**DEMO_LINE)
        db.add(line)
        db.flush()

        for order, station in enumerate(stations, start=1):
            db.add(LineStation(
                line_id=line.id,
                station_id=station.id,
                station_order=order,
                distance_from_previous=2.5 if order > 1 else 0,
            ))

        trains = [Train(**data) for data in DEMO_TRAINS]
        db.add_all(trains)
        db.flush()

        base_hour = 6
        for t_index, train in enumerate(trains):
            for s_index, station in enumerate(stations):
                hour = (base_hour + t_index * 2 + s_index) % 24
                arrival = time(hour=hour, minute=(s_index * 7) % 60)
                departure = time(hour=hour, minute=(arrival.minute + 2) % 60)
                is_peak = 8 <= hour <= 11 or 17 <= hour <= 20

                db.add(TrainSchedule(
                    train_id=train.id,
                    station_id=station.id,
                    arrival_time=arrival,
                    departure_time=departure,
                    platform_number=(s_index % 2) + 1,
                    day_type=DayType.WEEKDAY,
                    is_peak_hour=is_peak,
                    frequency_minutes=5 if is_peak else 12,
                ))

        for station in stations:
            db.add(CrowdLog(
                station_id=station.id,
                current_count=int(station.capacity * 0.2),
            ))

        db.commit()
        print(
            f"Seeded {len(stations)} stations, 1 line, {len(trains)} trains, "
            f"schedules and initial crowd logs.\n"
            f"No login was created - sign up via the frontend (Supabase), "
            f"then run:\n"
            f"  python -m app.database.set_user_role <your-email> admin"
        )

    finally:
        db.close()

if __name__ == "__main__":
    seed()
