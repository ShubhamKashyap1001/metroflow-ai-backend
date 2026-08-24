"""One-off migration for the new Alert columns (available_until,
notify_email, notify_sms).

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never alters an existing one. If your `alerts` table already
exists (i.e. you ran init_db.py before this update), run this once to
add the new columns without losing existing data:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_alert_columns

Safe to run more than once - every statement is IF NOT EXISTS. If you
still see `column "available_until" does not exist` in the API logs
after running this, the most likely cause is that this script and the
running `uvicorn` process are pointed at two different databases (e.g.
a local Postgres vs. a hosted one) - compare the "Connecting to"
line this script prints against DATABASE_URL in the .env the backend
is actually running with.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENTS = [
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS available_until TIMESTAMPTZ",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS notify_email BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS notify_sms BOOLEAN NOT NULL DEFAULT TRUE",
]

REQUIRED_COLUMNS = {"available_until", "notify_email", "notify_sms"}

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

        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'alerts'"
            )
        )
        existing_columns = {row[0] for row in result}

    missing = REQUIRED_COLUMNS - existing_columns
    if missing:
        print(
            f"\n⚠️  Still missing after running: {sorted(missing)}. "
            "The `alerts` table this script just altered does not have "
            "them - double check DATABASE_URL above is the same database "
            "your uvicorn/FastAPI process is using."
        )
    else:
        print("\n✅ Verified: `alerts` table now has available_until, notify_email, notify_sms.")
        print("Restart uvicorn (if it's running) and try creating an alert again.")

if __name__ == "__main__":
    run()