from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENTS = [
    "ALTER TABLE notification_logs ADD COLUMN IF NOT EXISTS job_id INTEGER "
    "REFERENCES notification_dispatch_jobs(id)",
    "CREATE INDEX IF NOT EXISTS ix_notification_logs_job_channel_recipient "
    "ON notification_logs (job_id, channel, recipient)",
]

REQUIRED_COLUMNS = {"job_id"}

def run():
    print(f"Connecting to: {settings.DATABASE_URL.split('@')[-1]}\n")

    with engine.begin() as conn:
        for statement in STATEMENTS:
            conn.execute(text(statement))
            print(f"OK: {statement}")

        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'notification_logs'"
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
        print("\n✅ Verified: `notification_logs` now has `job_id`.")
        print("Restart uvicorn (if it's running) for the new column to take effect.")

if __name__ == "__main__":
    run()
