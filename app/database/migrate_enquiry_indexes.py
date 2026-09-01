"""One-off migration to add the three indexes declared on
Enquiry.__table_args__ (app/models/enquiry.py):

    ix_enquiries_user_id_created_at
    ix_enquiries_status
    ix_enquiries_created_at

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. The
`enquiries` table originally shipped with NO indexes beyond its primary
key, so if it was created before this update, run this once so
enquiry_service.py's list_enquiries() (the "My Enquiries" page and the
admin "manage enquiries" queue) and get_enquiry_stats() stop doing a
full table scan on every call:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_enquiry_indexes

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on enquiries while building (relevant here since
create_enquiry()/resolve_enquiry() write to it) - note that
CONCURRENTLY can't run inside a transaction block, hence the
isolation_level="AUTOCOMMIT" connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEXES = [
    ("ix_enquiries_user_id_created_at", "user_id, created_at"),
    ("ix_enquiries_status", "status"),
    ("ix_enquiries_created_at", "created_at"),
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
                f"ON enquiries ({columns})"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {index_name}")

        result = conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'enquiries' AND indexname = ANY(:names)"
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
