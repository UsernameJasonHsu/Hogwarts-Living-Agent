import os
import shutil
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from hogwarts.db import make_engine
from hogwarts.main import create_app
from sqlalchemy import text
from sqlalchemy.engine import make_url

HUMAN_TOKEN = "test-human-" + "h" * 32
WORKER_TOKEN = "test-worker-" + "w" * 32


@pytest.fixture
def tmp_path():
    """Inherit workspace ACLs (Windows restricted-token runners cannot use mode 0700)."""
    path = Path("test-results/tmp") / uuid.uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path.resolve()
    finally:
        shutil.rmtree(path)


@pytest.fixture(params=["sqlite", "postgresql"])
def engine(request, tmp_path, monkeypatch):
    admin = None
    if request.param == "postgresql":
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("TEST_DATABASE_URL absent: PostgreSQL not verified")
        admin = make_engine(url)
        schema = "test_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = make_url(url).update_query_dict({"options": "-csearch_path=" + schema})
        url = url.render_as_string(hide_password=False)
    else:
        url = f"sqlite:///{tmp_path / 'governance.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(Config("alembic.ini"), "head")
    instance = make_engine(url)
    try:
        yield instance
    finally:
        instance.dispose()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


@pytest.fixture
def api(engine, monkeypatch):
    monkeypatch.setenv("HUMAN_API_TOKEN", HUMAN_TOKEN)
    monkeypatch.setenv("WORKER_API_TOKEN", WORKER_TOKEN)
    monkeypatch.setenv("OWNER_ID", "local-owner")
    monkeypatch.setenv("WORKER_AGENT_KEY", "engineer")
    with TestClient(create_app(engine)) as client:
        yield client


def headers(role="human", key=None):
    return {
        "Authorization": "Bearer " + (HUMAN_TOKEN if role == "human" else WORKER_TOKEN),
        "Idempotency-Key": key or uuid.uuid4().hex,
    }


def post(api, path, body, role="human", key=None):
    return api.post("/api/v1/" + path, json=body, headers=headers(role, key))


@pytest.fixture
def seed(api):
    project = post(
        api,
        "projects",
        {
            "repo_ref": "https://github.com/example/authorized.git",
            "allowed_paths": ["src", "tests"],
            "preapproved": True,
        },
    )
    agent = post(api, "agents", {"key": "engineer", "display_name": "Engineer"})
    assert project.status_code == agent.status_code == 200
    return {
        "project_id": project.json()["id"],
        "author_agent_id": agent.json()["id"],
        "origin": "assigned",
        "title": "Small fixture change",
        "problem": {"description": "Reproducible example", "evidence": []},
        "prototype": {
            "base_commit": "a" * 40,
            "allowed_paths": ["src/example.py"],
            "max_runtime_seconds": 60,
        },
        "acceptance_criteria": ["fixture behavior is deterministic"],
        "estimate_cents": 10,
    }
