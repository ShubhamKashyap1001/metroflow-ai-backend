"""One-off migration for the new `notification_dispatch_jobs` table
(the durable dispatch queue - see
app/models/notification_dispatch_job.py and
app/services/notification_dispatch_queue.py).

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet. That means create_all *would* pick this table up automatically on
a fresh database, but for an existing deployment (one where init_db.py
already ran before this update) that table simply won't exist until
something calls create_all again. Run this once to create it without
touching any other table:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_notification_dispatch_jobs

Safe to run more than once - create_all only creates a table if it's
missing.
"""
from app.core.config import settings
from app.database.base import Base
from app.database.database import engine
from app.models import NotificationDispatchJob  # noqa: F401 - registers the table on Base.metadata

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
    print("Creating notification_dispatch_jobs (if missing)...")
    Base.metadata.create_all(bind=engine, tables=[NotificationDispatchJob.__table__])
    print("\n✅ Verified: `notification_dispatch_jobs` table exists.")
    print("Restart uvicorn (if it's running) for the new table to take effect.")

if __name__ == "__main__":
    run()
