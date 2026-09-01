"""One-off migration to add the (station_id, created_at) index on
crowd_logs.

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. If your
`crowd_logs` table was created before this update, run this once so the
dashboard/heatmap/congestion queries (which all read crowd_logs by
station_id ordered by created_at) stop doing a full table scan:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_crowd_logs_index

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on crowd_logs while building (relevant here since the
crowd simulator/train tracker write to it every few seconds) - note that
CONCURRENTLY can't run inside a transaction block, hence the
isolation_level="AUTOCOMMIT" connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEX_NAME = "ix_crowd_logs_station_id_created_at"

STATEMENT = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
    "ON crowd_logs (station_id, created_at)"
)

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
        print(f"Running: {STATEMENT}")
        conn.execute(text(STATEMENT))
        print(f"OK: {INDEX_NAME}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'crowd_logs' AND indexname = :name"
            ),
            {"name": INDEX_NAME},
        )
        found = result.first() is not None

    if found:
        print(f"\n✅ Verified: `{INDEX_NAME}` exists on crowd_logs.")
    else:
        print(
            f"\n⚠️  `{INDEX_NAME}` was not found after running the "
            "statement above - double check DATABASE_URL matches the "
            "database your uvicorn process is actually using."
        )

if __name__ == "__main__":
    run()
