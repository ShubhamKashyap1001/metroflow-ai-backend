from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENTS = [
    "ALTER TABLE notification_read_states ADD COLUMN IF NOT EXISTS binned_at TIMESTAMPTZ",
    "ALTER TABLE notification_read_states ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
    "ALTER TABLE notification_read_states ALTER COLUMN read_at DROP NOT NULL",
]

REQUIRED_COLUMNS = {"binned_at", "deleted_at"}

def run():
    print(f"Connecting to: {settings.DATABASE_URL.split('@')[-1]}\n")

    with engine.begin() as conn:
        for statement in STATEMENTS:
            conn.execute(text(statement))
            print(f"OK: {statement}")

        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'notification_read_states'"
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
        print("\n✅ Verified: `notification_read_states` now has `binned_at` and `deleted_at`.")
        print("Restart uvicorn (if it's running) for the new columns to take effect.")

if __name__ == "__main__":
    run()
