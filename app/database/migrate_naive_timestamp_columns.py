"""One-off migration: convert the columns that used to be naive
`TIMESTAMP` (no time zone) over to `TIMESTAMP WITH TIME ZONE`:

    journeys.checkin_time
    journeys.checkout_time
    routes.created_at

BUGFIX (naive/aware datetime mixing): app/models/journey.py and
app/models/route.py declared these with plain `DateTime` while every
other timestamp column in the project (see app/mixins/timestamp.py's
TimestampMixin, and the DateTime(timezone=True) columns on Alert,
Prediction, Notification, etc.) uses `DateTime(timezone=True)`. The
application code was always writing timezone-aware UTC values
(`datetime.now(timezone.utc)`) into these naive columns - Postgres
silently drops the offset on the way in, and every value read back
out is a naive datetime that gets JSON-serialized with no UTC offset
(e.g. "2026-09-03T10:15:30" instead of "...+00:00"), which the
frontend's `new Date(...)` calls then parse as *local browser time*
instead of UTC.

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never alters a column type on a table that already exists. If
your `journeys`/`routes` tables were created before this update, run
this once:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_naive_timestamp_columns

Safe to run more than once - `ALTER COLUMN ... TYPE` is idempotent
(re-running it against an already-TIMESTAMPTZ column is a no-op other
than a quick catalog check). `USING <col> AT TIME ZONE 'UTC'`
reinterprets each existing naive value as UTC (which is what every
naive value in these columns actually was, since the app only ever
wrote `datetime.now(timezone.utc)`/`datetime.utcnow()` into them) -
no historical data is shifted, only the column's type/label changes.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

COLUMNS = [
    ("journeys", "checkin_time"),
    ("journeys", "checkout_time"),
    ("routes", "created_at"),
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
        existing_tables = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list({table for table, _ in COLUMNS})},
            )
        }

        for table, column in COLUMNS:
            if table not in existing_tables:
                print(f"SKIP: table {table!r} doesn't exist yet (create_tables() will make it "
                      f"timezone-aware from the start)")
                continue
            statement = (
                f"ALTER TABLE {table} "
                f"ALTER COLUMN {column} TYPE TIMESTAMP WITH TIME ZONE "
                f"USING {column} AT TIME ZONE 'UTC'"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {table}.{column}")

        result = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND (table_name, column_name) IN "
                "(SELECT unnest(:tables), unnest(:cols))"
            ),
            {
                "tables": [t for t, _ in COLUMNS],
                "cols": [c for _, c in COLUMNS],
            },
        )
        found = {(row[0], row[1]): row[2] for row in result}

    print()
    for table, column in COLUMNS:
        data_type = found.get((table, column))
        if data_type is None:
            print(f"⚠️  {table}.{column}: not found (table may not exist yet)")
        elif data_type == "timestamp with time zone":
            print(f"✅ {table}.{column}: {data_type}")
        else:
            print(f"⚠️  {table}.{column}: still {data_type!r} - migration may not have applied")

if __name__ == "__main__":
    run()
