from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEX_NAME = "ix_crowd_logs_station_id_created_at"

STATEMENT = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
    "ON crowd_logs (station_id, created_at)"
)

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
        print(f"Running: {STATEMENT}")
        conn.execute(text(STATEMENT))
        print(f"OK: {INDEX_NAME}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'crowd_logs' AND indexname = :name"
            ),
            {"name": INDEX_NAME},
        )
        found = result.first() is not None

    if found:
        print(f"\n✅ Verified: `{INDEX_NAME}` exists on crowd_logs.")
    else:
        print(
            f"\n⚠️  `{INDEX_NAME}` was not found after running the "
            "statement above - double check DATABASE_URL matches the "
            "database your uvicorn process is actually using."
        )

if __name__ == "__main__":
    run()
