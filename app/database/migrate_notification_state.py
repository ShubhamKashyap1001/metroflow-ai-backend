"""One-off migration for the new Notification.state column.

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never alters an existing one. If your `notifications` table
already exists (i.e. you ran init_db.py before this update), run this
once to add the new column without losing existing data:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_notification_state

Safe to run more than once - the statement is IF NOT EXISTS. Existing
rows get state = NULL, which means "not tied to any one state" - they
keep showing up for every user regardless of their selected state
filter, same as before this migration.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENTS = [
    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS state VARCHAR(50)",
]

REQUIRED_COLUMNS = {"state"}

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
                "WHERE table_name = 'notifications'"
            )
        )
        existing_columns = {row[0] for row in result}

    missing = REQUIRED_COLUMNS - existing_columns
    if missing:
        print(
            f"\n⚠️  Still missing after running: {sorted(missing)}. "
            "The `notifications` table this script just altered does not "
            "have them - double check DATABASE_URL above is the same "
            "database your uvicorn/FastAPI process is using."
        )
    else:
        print("\n✅ Verified: `notifications` table now has `state`.")
        print("Restart uvicorn (if it's running) for the new column to take effect.")

if __name__ == "__main__":
    run()
