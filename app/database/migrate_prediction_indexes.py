"""One-off migration to add the indexes declared on
Prediction.__table_args__ (app/models/prediction.py):

    ix_predictions_created_at
    ix_predictions_station_id_type_target

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. The
`predictions` table originally shipped with NO indexes beyond its
primary key, so if it was created before this update, run this once so:

  - analytics_service.py::prediction_insights() (the AI Prediction
    dashboard's "recent predictions" feed) stops doing a full table
    sort of the entire predictions history on every call
    (ix_predictions_created_at), and
  - prediction_service.py::_get_or_save_prediction()'s get-or-create
    lookup (see its docstring - the fix for unnecessary duplicate
    prediction writes on a cache hit) doesn't itself become a full
    table scan on every cache hit (ix_predictions_station_id_type_target)

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_prediction_indexes

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on predictions while building (relevant here since
predictions are written continuously) - note that CONCURRENTLY can't
run inside a transaction block, hence the isolation_level="AUTOCOMMIT"
connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEXES = [
    ("ix_predictions_created_at", "created_at"),
    ("ix_predictions_station_id_type_target", "station_id, prediction_type, target_datetime"),
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
                f"ON predictions ({columns})"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {index_name}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'predictions' AND indexname = ANY(:names)"
            ),
            {"names": [name for name, _ in INDEXES]},
        )
        found = {row[0] for row in result}

    print()
    for index_name, _ in INDEXES:
        mark = "OK" if index_name in found else "MISSING"
        print(f"{mark}: {index_name}")
    missing = [name for name, _ in INDEXES if name not in found]
    if missing:
        print(
            "\nSome indexes were not found after running the statements above - "
            "double check DATABASE_URL matches the database your uvicorn "
            "process is actually using."
        )

if __name__ == "__main__":
    run()
