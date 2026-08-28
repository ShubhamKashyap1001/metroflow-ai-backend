from collections import Counter
from datetime import datetime

from sqlalchemy import func

from app.database.database import engine
from app.database.session import SessionLocal
from app.enums.day_type import DayType
from app.models.station import Station
from app.models.train_schedule import TrainSchedule

def run():
    db = SessionLocal()
    try:
        total_stations = db.query(func.count(Station.id)).scalar()
        total_schedules = db.query(func.count(TrainSchedule.id)).scalar()
        print(f"Stations total: {total_stations}")
        print(f"TrainSchedule rows total: {total_schedules}\n")

        if total_schedules == 0:
            print("⚠️  train_schedules is completely empty. Run the seed script:")
            print("    python -m app.database.seed_real_data --reset")
            return

        # Rows per city (join through station_id), split by day_type
        rows = (
            db.query(Station.city, TrainSchedule.day_type, func.count(TrainSchedule.id))
            .join(TrainSchedule, TrainSchedule.station_id == Station.id)
            .group_by(Station.city, TrainSchedule.day_type)
            .all()
        )

        by_city: dict[str, Counter] = {}
        for city, day_type, count in rows:
            by_city.setdefault(city, Counter())[day_type.value] = count

        today_day_type = DayType.WEEKEND if datetime.now().weekday() >= 5 else DayType.WEEKDAY
        print(f"Today's day_type filter used by /schedules/upcoming: {today_day_type.value}\n")

        print(f"{'City':<15} {'weekday':>10} {'weekend':>10} {'holiday':>10}")
        for city in sorted(by_city):
            c = by_city[city]
            print(f"{city:<15} {c.get('weekday', 0):>10} {c.get('weekend', 0):>10} {c.get('holiday', 0):>10}")

        cities_with_no_schedules = set()
        all_cities = {c for (c,) in db.query(Station.city).distinct().all()}
        for city in all_cities:
            if city not in by_city:
                cities_with_no_schedules.add(city)

        print()
        if cities_with_no_schedules:
            print(f"⚠️  Cities with stations but ZERO train_schedules rows: {sorted(cities_with_no_schedules)}")
            print("   These stations exist but no schedule data was seeded for them -")
            print("   likely train_operations.csv.gz didn't have matching station_id/train_id")
            print("   rows for that city, so they were dropped during seeding (see the")
            print("   'dropped' counter printed by seed_real_data.py).")
        else:
            print("✅ Every city with stations has at least some train_schedules rows.")

        cities_missing_today = [
            city for city, c in by_city.items() if c.get(today_day_type.value, 0) == 0
        ]
        if cities_missing_today:
            print(f"\n⚠️  Cities with schedules, but NONE for today's day_type ({today_day_type.value}): "
                  f"{sorted(cities_missing_today)}")
            print("   /schedules/upcoming will show \"No departures found\" for these until")
            print("   that day type has data too (e.g. only weekend rows were seeded).")
    finally:
        db.close()

if __name__ == "__main__":
    run()