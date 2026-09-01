"""One-off migration for the new UserProfile.username column.

Same situation as migrate_alert_columns.py - this project has no
Alembic, so Base.metadata.create_all() never alters a `user_profiles`
table that already exists. If you already had users signing up before
this update, run this once:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_username_column

Safe to run more than once (IF NOT EXISTS). Starting fresh with a new
database? You don't need this - init_db.py already creates the column.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENT = "ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS username VARCHAR(50)"

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
        conn.execute(text(STATEMENT))
        print(f"OK: {STATEMENT}")

        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'user_profiles'"
            )
        )
        existing_columns = {row[0] for row in result}

    if "username" not in existing_columns:
        print(
            "\n⚠️  Still missing after running - double check DATABASE_URL "
            "above is the same database your uvicorn/FastAPI process uses."
        )
    else:
        print("\n✅ Verified: `user_profiles` table now has `username`.")
        print("Restart uvicorn (if it's running) and try signing up again.")

if __name__ == "__main__":
    run()
