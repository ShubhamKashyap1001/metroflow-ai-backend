"""One-off migration to add the alert/notification indexes declared in
(app/models/alert.py, app/models/notification.py,
app/models/notification_log.py):

    ix_alerts_created_at
    ix_alerts_station_id_created_at
    ix_alerts_is_resolved_created_at
    ix_notifications_created_at_is_read_user_id
    ix_notification_logs_alert_id_created_at

Same rationale/pattern as migrate_enquiry_indexes.py,
migrate_journey_indexes.py, etc.: this project doesn't use Alembic -
app/database/init_db.py just calls Base.metadata.create_all(), which
only creates tables that don't exist yet and never adds an index to a
table that already exists. These three tables originally shipped with
NO indexes beyond their primary key, so if they were created before
this update, run this once so alert_service.list_alerts(),
notification_service.list_notifications()/unread_count(), and
alert_service.list_alert_notifications() stop doing a full table scan
on every call. See docs/query-performance-and-indexing.md for the before/after
EXPLAIN ANALYZE numbers this was based on.

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_alert_notification_indexes

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so
it doesn't lock writes on these tables while building (alerts/
notifications/notification_logs are all written to continuously by
live traffic) - note that CONCURRENTLY can't run inside a transaction
block, hence the isolation_level="AUTOCOMMIT" connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEXES = [
    ("ix_alerts_created_at", "alerts", "created_at"),
    ("ix_alerts_station_id_created_at", "alerts", "station_id, created_at"),
    ("ix_alerts_is_resolved_created_at", "alerts", "is_resolved, created_at"),
    ("ix_notifications_created_at_is_read_user_id", "notifications", "created_at, is_read, user_id"),
    ("ix_notification_logs_alert_id_created_at", "notification_logs", "alert_id, created_at"),
]

def _masked_database_url() -> str:
    url = settings.DATABASE_URL
    if "@" in url and "//" in url:
        scheme, rest = url.split("//", 1)
        creds, rest = rest.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{scheme}//{user}:***@{rest}"
    return url

def run():
    print(f"Connecting to: {_masked_database_url()}\n")

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for index_name, table, columns in INDEXES:
            statement = (
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON {table} ({columns})"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {index_name}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE indexname = ANY(:names)"
            ),
            {"names": [name for name, _, _ in INDEXES]},
        )
        found = {row[0] for row in result}

    print()
    for index_name, _, _ in INDEXES:
        mark = "\u2705" if index_name in found else "\u26a0\ufe0f "
        print(f"{mark} {index_name}")
    missing = [name for name, _, _ in INDEXES if name not in found]
    if missing:
        print(
            "\nSome indexes were not found after running the statements above - "
            "double check DATABASE_URL matches the database your uvicorn "
            "process is actually using."
        )

if __name__ == "__main__":
    run()
