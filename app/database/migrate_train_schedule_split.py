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
