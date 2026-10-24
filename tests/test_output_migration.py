"""Isolated SQLite/PostgreSQL schema roundtrip; never a deployment database."""

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect


def test_output_import_migration_roundtrip_and_no_model_drift(engine):
    config = Config("alembic.ini")
    tables = set(inspect(engine).get_table_names())
    assert "output_imports" in tables
    columns = {c["name"]: c for c in inspect(engine).get_columns("output_imports")}
    assert columns["sandbox_id"]["type"].length == 36
    assert columns["manifest_artifact_id"]["type"].length == 36
    command.check(config)
    command.downgrade(config, "0005")
    assert set(inspect(engine).get_table_names()) == tables - {"output_imports"}
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) == tables
    command.check(config)
