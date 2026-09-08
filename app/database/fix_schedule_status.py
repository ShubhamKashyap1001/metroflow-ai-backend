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

BUGFIX: the raw SQL below now uses 'DELAYED'/'ON_TIME' (matching the
native Postgres enum labels SQLAlchemy actually creates for
ScheduleStatus.DELAYED/ScheduleStatus.ON_TIME - `Enum(ScheduleStatus)`
in app/models/train_schedule.py has no `values_callable`, so those
labels are the enum members' NAMES, not their lowercase `.value`s; see
the matching BUGFIX comment on Journey.__table_args__ in
app/models/journey.py for the full explanation). The previous
lowercase 'delayed'/'on_time' literals were not valid labels of the
schedulestatus enum type at all, so every statement below failed
outright with `invalid input value for enum schedulestatus: "delayed"`
on any real Postgres database.

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
                "UPDATE train_schedules SET status = 'DELAYED' "
                "WHERE delay_minutes > 0 AND status != 'DELAYED'"
            )
        )
        print(f"OK: {delayed.rowcount} row(s) with real delay_minutes > 0 flipped to status='DELAYED'.")

        on_time = conn.execute(
            text(
                "UPDATE train_schedules SET status = 'ON_TIME' "
                "WHERE delay_minutes = 0 AND status = 'DELAYED'"
            )
        )
        print(f"OK: {on_time.rowcount} stale row(s) with delay_minutes = 0 reset to status='ON_TIME'.")

        remaining = conn.execute(
            text("SELECT COUNT(*) FROM train_schedules WHERE delay_minutes > 0")
        ).scalar()
        now_delayed = conn.execute(
            text("SELECT COUNT(*) FROM train_schedules WHERE status = 'DELAYED'")
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
