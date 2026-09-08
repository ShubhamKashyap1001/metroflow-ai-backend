"""Diagnostic: how many train_schedules rows exist per city, and for
today's day_type. Run this to check whether "No departures found for
<city>" is a real data-coverage gap (seed data has no schedule rows
for that city's stations) vs a bug.

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate
    python -m app.database.diagnose_schedule_coverage
"""
from collections import Counter

from sqlalchemy import func

from app.database.database import engine
from app.database.session import SessionLocal
from app.enums.day_type import DayType
from app.models.station import Station
from app.models.train_schedule import TrainSchedule
from app.utils.timezone import business_now

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

        # BUGFIX (naive datetime / timezone handling): this used to
        # resolve against the naive server-local clock, then (in a
        # follow-up fix) against raw UTC - both of which can disagree
        # with the actual day_type filter /schedules/upcoming applies
        # (see schedule_service.py::_current_day_type()), the former
        # depending on what timezone the machine running this
        # diagnostic happens to be in, the latter because schedule
        # rows are keyed on local business time, not UTC. Resolved
        # against the same business timezone as _current_day_type()
        # here too, so this diagnostic can never report a different
        # "today" than the API it's meant to be diagnosing.
        today_day_type = DayType.WEEKEND if business_now().weekday() >= 5 else DayType.WEEKDAY
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