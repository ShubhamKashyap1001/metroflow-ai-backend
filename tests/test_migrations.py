
import os

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from app.database.base import Base
from app.models import *  # noqa: F401,F403

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def alembic_config(tmp_path):
    db_path = tmp_path / "migration_test.db"
    db_url = f"sqlite:///{db_path}"
    cfg = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(REPO_ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.attributes["configure_logger"] = False
    return cfg, db_url


def test_single_linear_head_revision_no_branching(alembic_config):
    """Deterministic & ordered: exactly one head, no ambiguous branch
    Alembic could apply out of order."""
    cfg, _ = alembic_config
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1


def test_upgrade_head_on_fresh_database_creates_expected_tables(alembic_config):
    """Fresh database (no tables, no alembic_version yet): running the
    migration once creates the full current schema in one step."""
    cfg, db_url = alembic_config
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    for expected in ("stations", "journeys", "trains", "predictions", "alembic_version"):
        assert expected in tables


def test_upgrade_head_is_idempotent_and_records_current_revision(alembic_config):
    """Running the exact same migration twice in a row (e.g. a
    redeployment re-running `alembic upgrade head`) must not error and
    must leave the database on the same, single, correctly recorded
    revision."""
    cfg, db_url = alembic_config
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # must not raise

    engine = create_engine(db_url)
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert current == [("0001_baseline_schema",)]


def test_upgrade_head_does_not_touch_pre_existing_data(alembic_config):
    """The critical production-safety case: a database that already
    has these tables (e.g. created by the old
    Base.metadata.create_all()-only flow, or by a previous
    `alembic upgrade head`) and already has real rows in it must come
    through `alembic upgrade head` with that data completely intact -
    no table recreated, no row dropped."""
    cfg, db_url = alembic_config

    # Simulate a pre-existing production database: tables already
    # created (as init_db.py's create_tables() would do), with a real
    # row already in it, and NO alembic_version table yet - i.e. this
    # database predates the migration process entirely.
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO stations "
                "(station_code, station_name, city, latitude, longitude, "
                "capacity, is_interchange, is_active) "
                "VALUES ('PROD1', 'Production Station', 'ProdCity', "
                "1.0, 1.0, 500, 0, 1)"
            )
        )

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM stations")).scalar() == 1
        alembic_version_exists = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
        ).fetchall()
    assert alembic_version_exists == []

    # Bring this "production" database under Alembic for the first time.
    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT station_code FROM stations")).fetchall()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert rows == [("PROD1",)]
    assert version == [("0001_baseline_schema",)]
