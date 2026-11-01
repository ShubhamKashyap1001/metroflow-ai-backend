from app.core.config import settings
from app.database.base import Base
from app.database.database import engine
from app.models import NotificationDispatchJob  # noqa: F401 - registers the table on Base.metadata

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
    print("Creating notification_dispatch_jobs (if missing)...")
    Base.metadata.create_all(bind=engine, tables=[NotificationDispatchJob.__table__])
    print("\n✅ Verified: `notification_dispatch_jobs` table exists.")
    print("Restart uvicorn (if it's running) for the new table to take effect.")

if __name__ == "__main__":
    run()
