from sqlalchemy import text

from app.core.config import settings
from app.database.database import engine

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
        delayed = conn.execute(
            text(
                "UPDATE train_schedules SET status = 'DELAYED' "
                "WHERE delay_minutes > 0 AND status != 'DELAYED'"
            )
        )
        print(f"OK: {delayed.rowcount} row(s) with real delay_minutes > 0 flipped to status='DELAYED'.")

        on_time = conn.execute(
            text(
                "UPDATE train_schedules SET status = 'ON_TIME' "
                "WHERE delay_minutes = 0 AND status = 'DELAYED'"
            )
        )
        print(f"OK: {on_time.rowcount} stale row(s) with delay_minutes = 0 reset to status='ON_TIME'.")

        remaining = conn.execute(
            text("SELECT COUNT(*) FROM train_schedules WHERE delay_minutes > 0")
        ).scalar()
        now_delayed = conn.execute(
            text("SELECT COUNT(*) FROM train_schedules WHERE status = 'DELAYED'")
        ).scalar()

    print(
        f"\nVerified: {remaining} schedule row(s) have delay_minutes > 0, "
        f"{now_delayed} row(s) now have status='delayed'."
    )
    if remaining == now_delayed:
        print("✅ status and delay_minutes are now consistent. Restart uvicorn and refresh the dashboard.")
    else:
        print("⚠️  Counts still don't match - re-run this script, or check for a second DB/connection mismatch.")

if __name__ == "__main__":
    run()
