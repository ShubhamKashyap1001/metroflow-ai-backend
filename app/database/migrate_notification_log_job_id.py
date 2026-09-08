"""One-off migration for `notification_logs` gaining a nullable
`job_id` column (see app/models/notification_log.py) - the fix for
duplicate email/SMS sends when a notification dispatch job is resumed
or re-executed after a process crash/restart (see
app/services/notification_dispatch_queue.py and
app/models/notification_dispatch_job.py).

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never alters an existing one. If you already ran init_db.py
(or an earlier notification_logs migration) before this update, run
this once:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_notification_log_job_id

Safe to run more than once - every statement is IF NOT EXISTS, which is
a no-op if already applied. Existing rows get job_id = NULL, i.e.
"logged before this fix / not tied to a specific dispatch job" -
behaviourally identical to before this migration (they're simply never
matched by the new idempotency check, exactly like any other dispatch
that predates it).
"""
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
