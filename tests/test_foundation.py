from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hogwarts.db import make_engine
from hogwarts.main import create_app
from sqlalchemy import inspect


def test_health(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'health.db'}")
    with TestClient(create_app(engine)) as client:
        assert client.get("/health").json()["status"] == "ok"
    engine.dispose()


def test_migrations_up_down(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = make_engine(url)
    assert "alembic_version" in inspect(engine).get_table_names()
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(config, "head")
    engine.dispose()


def test_full_migration_roundtrip_and_no_drift(engine):
    config = Config("alembic.ini")
    tables = set(inspect(engine).get_table_names())
    assert {
        "agents",
        "proposals",
        "projects",
        "prototype_runs",
        "approval_requests",
        "budget_reservations",
        "workflow_events",
    } <= tables
    command.check(config)
    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) == tables
