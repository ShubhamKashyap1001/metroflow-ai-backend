from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

STATEMENTS = [
    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS state VARCHAR(50)",
]

REQUIRED_COLUMNS = {"state"}

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

    with engine.begin() as conn:
        for statement in STATEMENTS:
            conn.execute(text(statement))
            print(f"OK: {statement}")

        result = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'notifications'"
            )
        )
        existing_columns = {row[0] for row in result}

    missing = REQUIRED_COLUMNS - existing_columns
    if missing:
        print(
            f"\n⚠️  Still missing after running: {sorted(missing)}. "
            "The `notifications` table this script just altered does not "
            "have them - double check DATABASE_URL above is the same "
            "database your uvicorn/FastAPI process is using."
        )
    else:
        print("\n✅ Verified: `notifications` table now has `state`.")
        print("Restart uvicorn (if it's running) for the new column to take effect.")

if __name__ == "__main__":
    run()
