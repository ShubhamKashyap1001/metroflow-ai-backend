"""One-off migration for the timetable/history split (see
app/models/train_schedule_history.py for the full rationale).

Does two things against an EXISTING database:

  1. Adds `station_sequence` to the existing `train_schedules` table
     (Base.metadata.create_all only creates missing tables - it never
     adds a column to a table that already exists, same limitation
     documented in migrate_train_schedule_indexes.py).
  2. Creates the new `train_schedule_history` table, if it doesn't
     exist yet (create_all *would* pick this one up automatically on
     a fresh DB, but running it explicitly here means this single
     script is a complete, one-command upgrade path for an existing
     deployment - no need to also remember to run init_db.py).

Run once:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_train_schedule_split

Safe to run more than once - every statement is IF NOT EXISTS.

This does NOT backfill station_sequence on existing train_schedules
rows, and does NOT move any existing data into train_schedule_history.
For a database that was seeded before this migration, re-run
`python -m app.database.seed_real_data --reset` afterwards to get a
clean split (see that script's own docstring) - this migration only
makes the schema ready to receive that reseed.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.base import Base
from app.database.database import engine
from app.models import TrainScheduleHistory  # noqa: F401 - registers the table on Base.metadata


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

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        print("Adding train_schedules.station_sequence (if missing)...")
        conn.execute(
            text(
                "ALTER TABLE train_schedules "
                "ADD COLUMN IF NOT EXISTS station_sequence INTEGER"
            )
        )
        print("OK: station_sequence")

    print("\nCreating train_schedule_history (if missing)...")
    Base.metadata.create_all(bind=engine, tables=[TrainScheduleHistory.__table__])
    print("OK: train_schedule_history")

    print(
        "\nSchema is ready. If this database was already seeded from "
        "train_operations.csv before this migration, re-run:\n"
        "    python -m app.database.seed_real_data --reset\n"
        "to split the old duplicated rows into a clean timetable + "
        "full history."
    )


if __name__ == "__main__":
    run()
