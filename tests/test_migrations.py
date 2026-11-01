
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
    cfg, _ = alembic_config
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1

def test_upgrade_head_on_fresh_database_creates_expected_tables(alembic_config):
    cfg, db_url = alembic_config
    command.upgrade(cfg, "head")

    engine = create_engine(db_url)
    tables = set(inspect(engine).get_table_names())
    for expected in ("stations", "journeys", "trains", "predictions", "report_schedules", "alembic_version"):
        assert expected in tables

def test_upgrade_head_is_idempotent_and_records_current_revision(alembic_config):
    cfg, db_url = alembic_config
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  

    engine = create_engine(db_url)
    with engine.connect() as conn:
        current = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert current == [("0006_reports_and_freshness",)]

def test_upgrade_head_does_not_touch_pre_existing_data(alembic_config):
    cfg, db_url = alembic_config

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

    command.upgrade(cfg, "head")

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT station_code FROM stations")).fetchall()
        version = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
    assert rows == [("PROD1",)]
    assert version == [("0006_reports_and_freshness",)]
