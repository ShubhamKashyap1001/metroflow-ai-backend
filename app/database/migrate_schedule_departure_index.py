"""One-off migration to add the index declared on
TrainSchedule.__table_args__ (app/models/train_schedule.py):

    ix_train_schedules_day_type_departure_time

Added after a query-analysis pass (docs/query-performance-and-indexing.md)
identified schedule_service.get_upcoming_schedules() - the Dispatch
Board's "Upcoming Train Schedule" widget - as the slowest verified
query in the app under realistic data volume. It filters
train_schedules on day_type + departure_time and orders by
departure_time; none of the pre-existing indexes include
departure_time at all, so every call did a full Seq Scan followed by
an explicit Sort. This composite index lets Postgres satisfy both the
filter and the ORDER BY straight from the index.

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. If your
`train_schedules` table was created before this update, run this once:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_schedule_departure_index

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on train_schedules while building (relevant here
since handle_delay()/adjust_frequency() write to it) - note that
CONCURRENTLY can't run inside a transaction block, hence the
isolation_level="AUTOCOMMIT" connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEXES = [
    ("ix_train_schedules_day_type_departure_time", "day_type, departure_time"),
]


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
        for index_name, columns in INDEXES:
            statement = (
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON train_schedules ({columns})"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {index_name}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'train_schedules' AND indexname = ANY(:names)"
            ),
            {"names": [name for name, _ in INDEXES]},
        )
        found = {row[0] for row in result}

    print()
    for index_name, _ in INDEXES:
        mark = "\u2705" if index_name in found else "\u26a0\ufe0f "
        print(f"{mark} {index_name}")
    missing = [name for name, _ in INDEXES if name not in found]
    if missing:
        print(
            "\nSome indexes were not found after running the statements above - "
            "double check DATABASE_URL matches the database your uvicorn "
            "process is actually using."
        )


if __name__ == "__main__":
    run()
