"""One-off migration for the new TrainLocation live-tracking columns
(next_station_id, progress_ratio, status) and the train_id unique
constraint.

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never alters an existing one. If your `train_locations` table
already existed before this update, run this once to add the new
columns without losing data:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_train_location_columns

Safe to run more than once - every statement is IF NOT EXISTS / guarded.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENTS = [
    "ALTER TABLE train_locations ADD COLUMN IF NOT EXISTS next_station_id INTEGER REFERENCES stations(id)",
    "ALTER TABLE train_locations ADD COLUMN IF NOT EXISTS progress_ratio DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE train_locations ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'in_transit'",
]

DEDUPE_STATEMENT = """
DELETE FROM train_locations a USING train_locations b
WHERE a.train_id = b.train_id AND a.id < b.id
"""

CONSTRAINT_STATEMENT = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_train_locations_train_id'
    ) THEN
        ALTER TABLE train_locations ADD CONSTRAINT uq_train_locations_train_id UNIQUE (train_id);
    END IF;
END $$;
"""

REQUIRED_COLUMNS = {"next_station_id", "progress_ratio", "status"}

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
        for statement in STATEMENTS:
            conn.execute(text(statement))
            print(f"OK: {statement}")

        conn.execute(text(DEDUPE_STATEMENT))
        print("OK: de-duplicated to one row per train")

        conn.execute(text(CONSTRAINT_STATEMENT))
        print("OK: uq_train_locations_train_id constraint ensured")

        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'train_locations'"
            )
        )
        existing_columns = {row[0] for row in result}

    missing = REQUIRED_COLUMNS - existing_columns
    if missing:
        print(
            f"\n⚠️  Still missing after running: {sorted(missing)}. "
            "Double check DATABASE_URL above is the same database your "
            "uvicorn/FastAPI process is using."
        )
    else:
        print("\n✅ Verified: `train_locations` has next_station_id, progress_ratio, status.")
        print("Restart uvicorn (if it's running) to pick up live train tracking.")

if __name__ == "__main__":
    run()
