"""Creates/upgrades the local database schema for dev/CI setup.

BUGFIX (single source of truth for schema): this used to call
`Base.metadata.create_all(bind=engine)` directly - the exact same
"schema creation happens in code, outside any tracked history" pattern
that motivated wiring up Alembic as the project's real migration
process in the first place (see alembic.ini's header comment). Having
both a real, version-tracked migration chain AND a separate script
that can independently create/evolve the schema is how the two end up
silently out of sync: a schema change added only as an ORM model
change (no new Alembic revision) would still reach a database that
ran this script, while `alembic upgrade head` - the actual production
path - would know nothing about it.

Alembic is now the only mechanism that creates or evolves the schema,
for a brand new database and an existing one alike. This just invokes
`alembic upgrade head` programmatically, so the familiar
`python -m app.database.init_db` command documented in README.md still
works for local/dev setup - it does exactly what running
`alembic upgrade head` from a shell in the project root would do.
migrations/versions/0001_baseline_schema.py's own explicit
`op.create_table`/`op.create_index` calls (each guarded by a check
against the connected database's actual current state) are what
actually create the tables/indexes on a fresh database,
non-destructively; an existing production database that already has
them is left completely untouched - no table or index is ever
created twice, altered, dropped, or has its data touched here.
"""
from pathlib import Path

from alembic import command
from alembic.config import Config

# app/database/init_db.py -> app/database -> app -> project root.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_tables() -> None:
    """Bring the configured database's schema up to date via Alembic
    (`alembic upgrade head`) instead of a bare `create_all()`. Safe to
    run repeatedly, and safe against a database that's already fully
    migrated - Alembic no-ops anything already applied.

    Uses an absolute script_location (rather than alembic.ini's
    relative `migrations`) so this behaves the same regardless of the
    working directory it's invoked from - `python -m
    app.database.init_db` doesn't require running from the project
    root the way the raw `alembic upgrade head` CLI flow does.
    """
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, "head")


if __name__ == "__main__":
    create_tables()
    print("✅ Database schema is up to date (alembic upgrade head)")
