from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEX_NAME = "ux_journeys_one_active_per_user"

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
        statement = (
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
            f"ON journeys (user_id) WHERE status = 'ACTIVE'"
        )
        print(f"Running: {statement}")
        conn.execute(text(statement))
        print(f"OK: {INDEX_NAME}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'journeys' AND indexname = :name"
            ),
            {"name": INDEX_NAME},
        )
        found = result.first() is not None

    print()
    mark = "✅" if found else "⚠️ "
    print(f"{mark} {INDEX_NAME}")
    if not found:
        print(
            "\nIndex was not found after running the statement above - "
            "double check DATABASE_URL matches the database your uvicorn "
            "process is actually using."
        )

if __name__ == "__main__":
    run()
