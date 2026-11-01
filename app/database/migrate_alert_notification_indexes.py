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
