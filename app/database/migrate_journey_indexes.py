"""One-off migration to add the four composite indexes declared on
Journey.__table_args__ (app/models/journey.py):

    ix_journeys_user_id_status
    ix_journeys_status_checkin_time
    ix_journeys_source_station_id_status
    ix_journeys_destination_station_id_status

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. If your
`journeys` table was created before this update, run this once so the
hot journey queries (duplicate-active-journey check, the live/CSV
simulators' checkout sweeps, per-station active-journey counts, etc. -
see the comment above Journey.__table_args__ for the full list) stop
doing a full table scan of an ever-growing table:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_journey_indexes

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on journeys while building (relevant here since
check-in/check-out and the simulators write to it constantly) - note
that CONCURRENTLY can't run inside a transaction block, hence the
isolation_level="AUTOCOMMIT" connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEXES = [
    ("ix_journeys_user_id_status", "user_id, status"),
    ("ix_journeys_status_checkin_time", "status, checkin_time"),
    ("ix_journeys_source_station_id_status", "source_station_id, status"),
    ("ix_journeys_destination_station_id_status", "destination_station_id, status"),
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
                f"ON journeys ({columns})"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {index_name}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'journeys' AND indexname = ANY(:names)"
            ),
            {"names": [name for name, _ in INDEXES]},
        )
        found = {row[0] for row in result}

    print()
    for index_name, _ in INDEXES:
        mark = "✅" if index_name in found else "⚠️ "
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
