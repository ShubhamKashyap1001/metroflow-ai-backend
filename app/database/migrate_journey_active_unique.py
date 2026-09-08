"""One-off migration to add the partial unique index declared on
Journey.__table_args__ (app/models/journey.py):

    ux_journeys_one_active_per_user

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. If your
`journeys` table was created before this update, run this once so the
database itself rejects a second ACTIVE journey for the same user
(closes a check-then-insert race where two simultaneous check-in
requests could otherwise both pass the "any active journey?" check and
both insert an ACTIVE row):

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_journey_active_unique

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on journeys while building (relevant here since
check-in/check-out write to it constantly) - note that CONCURRENTLY
can't run inside a transaction block, hence the
isolation_level="AUTOCOMMIT" connection below.

If duplicate ACTIVE rows already exist for some user (from the race
this migration closes), index creation will fail - resolve those rows
(e.g. complete/cancel all but the most recent per user) before
re-running.

BUGFIX: the predicate below now uses 'ACTIVE' (matching the native
Postgres enum label SQLAlchemy actually creates for JourneyStatus.ACTIVE
- see the BUGFIX comment on Journey.__table_args__ in
app/models/journey.py for the full explanation). The previous
lowercase 'active' was not a valid label of the journeystatus enum
type at all, so `CREATE UNIQUE INDEX ... WHERE status = 'active'`
failed outright with `invalid input value for enum journeystatus:
"active"` on any real Postgres database.
"""
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
