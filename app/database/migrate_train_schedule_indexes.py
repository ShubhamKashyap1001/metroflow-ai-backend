
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEXES = [
    ("ix_train_schedules_station_id_day_type", "station_id, day_type"),
    ("ix_train_schedules_station_id_status", "station_id, status"),
    ("ix_train_schedules_train_id", "train_id"),
]

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

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for index_name, columns in INDEXES:
            statement = (
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
                f"ON train_schedules ({columns})"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {index_name}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'train_schedules' AND indexname = ANY(:names)"
            ),
            {"names": [name for name, _ in INDEXES]},
        )
        found = {row[0] for row in result}

    print()
    for index_name, _ in INDEXES:
        mark = "✅" if index_name in found else "⚠️ "
        print(f"{mark} {index_name}")
    missing = [name for name, _ in INDEXES if name not in found]
    if missing:
        print(
            "\nSome indexes were not found after running the statements above - "
            "double check DATABASE_URL matches the database your uvicorn "
            "process is actually using."
        )

if __name__ == "__main__":
    run()
