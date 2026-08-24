"""One-off data fix for the "Average Delay always shows 0.0" bug.

Root cause: app/database/seed_real_data.py created every TrainSchedule
row from the real train_operations.csv delay values but never set
`status` - so every seeded row silently kept the column default
(ScheduleStatus.ON_TIME), no matter how large `delay_minutes` was.
The dashboard's "Average Delay" KPI reads from the
`/schedules/delayed` endpoint, which (before this fix) filtered on
`status == DELAYED` only, so it always returned 0 rows even though
real delay data was sitting in the DB the whole time.

app/database/seed_real_data.py and app/services/schedule_service.py
are both fixed going forward (new seeds set status correctly, and the
delayed-schedules query no longer depends on status alone). This
script is only needed if you already ran the seed script BEFORE that
fix and don't want to drop + reseed the database - it backfills
`status` on existing rows using their existing (real) delay_minutes,
no fake data involved.

Safe to run more than once.

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.fix_schedule_status
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

def _masked_database_url() -> str:
    url = settings.DATABASE_URL
    if "@" in url and "//" in url:
        scheme_and_creds, rest = url.split("@", 1)
        scheme, creds = scheme_and_creds.split("//", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}//{user}:***@{rest}"
    return url

def run():
    print(f"Connecting to: {_masked_database_url()}\n")

    with engine.begin() as conn:
        delayed = conn.execute(
            text(
                "UPDATE train_schedules SET status = 'delayed' "
                "WHERE delay_minutes > 0 AND status != 'delayed'"
            )
        )
        print(f"OK: {delayed.rowcount} row(s) with real delay_minutes > 0 flipped to status='delayed'.")

        on_time = conn.execute(
            text(
                "UPDATE train_schedules SET status = 'on_time' "
                "WHERE delay_minutes = 0 AND status = 'delayed'"
            )
        )
        print(f"OK: {on_time.rowcount} stale row(s) with delay_minutes = 0 reset to status='on_time'.")

        remaining = conn.execute(
            text("SELECT COUNT(*) FROM train_schedules WHERE delay_minutes > 0")
        ).scalar()
        now_delayed = conn.execute(
            text("SELECT COUNT(*) FROM train_schedules WHERE status = 'delayed'")
        ).scalar()

    print(
        f"\nVerified: {remaining} schedule row(s) have delay_minutes > 0, "
        f"{now_delayed} row(s) now have status='delayed'."
    )
    if remaining == now_delayed:
        print("✅ status and delay_minutes are now consistent. Restart uvicorn and refresh the dashboard.")
    else:
        print("⚠️  Counts still don't match - re-run this script, or check for a second DB/connection mismatch.")

if __name__ == "__main__":
    run()
