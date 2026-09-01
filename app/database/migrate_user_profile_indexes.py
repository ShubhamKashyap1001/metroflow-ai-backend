"""One-off migration to add the index declared on
UserProfile.__table_args__ (app/models/user_profile.py):

    ix_user_profiles_created_at

This project doesn't use Alembic - app/database/init_db.py just calls
Base.metadata.create_all(), which only creates tables that don't exist
yet and never adds an index to a table that already exists. The
`user_profiles` table originally shipped with no index beyond its
primary key and the implicit one from `email`'s `unique=True`, so if it
was created before this update, run this once so
app/api/v1/users.py::get_users() (the admin "User Management" list,
now paginated - see DEFAULT_USERS_LIMIT/MAX_USERS_LIMIT there) stops
doing a full table sort of user_profiles on every page:

    cd backend
    venv\\Scripts\\activate      (Windows)   or   source venv/bin/activate   (macOS/Linux)
    python -m app.database.migrate_user_profile_indexes

Safe to run more than once - uses IF NOT EXISTS. Uses CONCURRENTLY so it
doesn't lock writes on user_profiles while building - note that
CONCURRENTLY can't run inside a transaction block, hence the
isolation_level="AUTOCOMMIT" connection below.
"""
from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

INDEX_NAME = "ix_user_profiles_created_at"

STATEMENT = (
    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
    "ON user_profiles (created_at)"
)

def _masked_database_url() -> str:
    url = settings.DATABASE_URL
    if "@" in url and "//" in url:
        scheme_and_creds, rest = url.split("@", 1)
        scheme, creds = scheme_and_creds.split(":", 1)[0], rest
        return f"{scheme}//***:***@{rest}"
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
                "WHERE tablename = 'user_profiles' AND indexname = :name"
            ),
            {"name": INDEX_NAME},
        )
        found = result.first() is not None

    if found:
        print(f"\n✅ Verified: `{INDEX_NAME}` exists on user_profiles.")
    else:
        print(
            f"\n⚠️  `{INDEX_NAME}` was not found after running the "
            "statement above - double check DATABASE_URL matches the "
            "database your uvicorn process is actually using."
        )

if __name__ == "__main__":
    run()
