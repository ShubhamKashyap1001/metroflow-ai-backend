from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

COLUMNS = [
    ("journeys", "checkin_time"),
    ("journeys", "checkout_time"),
    ("routes", "created_at"),
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
        existing_tables = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = ANY(:names)"
                ),
                {"names": list({table for table, _ in COLUMNS})},
            )
        }

        for table, column in COLUMNS:
            if table not in existing_tables:
                print(f"SKIP: table {table!r} doesn't exist yet (create_tables() will make it "
                      f"timezone-aware from the start)")
                continue
            statement = (
                f"ALTER TABLE {table} "
                f"ALTER COLUMN {column} TYPE TIMESTAMP WITH TIME ZONE "
                f"USING {column} AT TIME ZONE 'UTC'"
            )
            print(f"Running: {statement}")
            conn.execute(text(statement))
            print(f"OK: {table}.{column}")

        result = conn.execute(
            text(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND (table_name, column_name) IN "
                "(SELECT unnest(:tables), unnest(:cols))"
            ),
            {
                "tables": [t for t, _ in COLUMNS],
                "cols": [c for _, c in COLUMNS],
            },
        )
        found = {(row[0], row[1]): row[2] for row in result}

    print()
    for table, column in COLUMNS:
        data_type = found.get((table, column))
        if data_type is None:
            print(f"⚠️  {table}.{column}: not found (table may not exist yet)")
        elif data_type == "timestamp with time zone":
            print(f"✅ {table}.{column}: {data_type}")
        else:
            print(f"⚠️  {table}.{column}: still {data_type!r} - migration may not have applied")

if __name__ == "__main__":
    run()
