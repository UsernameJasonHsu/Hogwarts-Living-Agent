"""Regression coverage for the three independently reproduced M1 acceptance findings."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock

import pytest
from conftest import headers, post
from fastapi.testclient import TestClient
from hogwarts.adapters import fake_adapters
from hogwarts.models import Agent, PrototypeRun, WorkflowEvent, WorkspaceBudget
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session
from test_governance import claimed, complete


def test_failed_sandbox_creation_must_not_emit_ready(api, seed, engine):
    adapters = fake_adapters()

    class FailedSandbox:
        def create(self, repository, commit, run_id):
            raise RuntimeError("simulated allocation failure")

        def destroy(self, sandbox_id):
            raise AssertionError("nothing was allocated")

    adapters.sandbox = FailedSandbox()
    api.app.state.adapters = adapters
    proposal, lease = claimed(api, seed)
    result = complete(api, proposal, lease)
    assert result.status_code == 200
    assert result.json()["status"] == "FAILED"
    events = api.get(f"/api/v1/prototypes/{proposal['id']}/events", headers=headers()).json()
    event_types = [item["event_type"] for item in events]
    assert "sandbox.requested" in event_types
    assert "sandbox.ready" not in event_types
    assert event_types[-1] == "run.failed"
    with Session(engine) as session:
        assert session.get(PrototypeRun, proposal["run_id"]).status == "FAILED"
        assert session.get(WorkspaceBudget, "local-owner").reserved_cents == 0
        assert session.get(Agent, seed["author_agent_id"]).active_runs == 0


@pytest.mark.parametrize("racing", [False, True], ids=["serial", "racing"])
def test_conflicting_idempotency_request_audits_exactly_one_denial(api, seed, engine, racing):
    barrier = Barrier(2)

    def synchronize_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith("insert into idempotency_records"):
            barrier.wait(timeout=10)

    def invoke(index):
        body = {**seed, "title": f"conflicting title {index}"}
        return post(api, "prototypes", body, key="regression-body-conflict")

    if racing:
        event.listen(engine, "before_cursor_execute", synchronize_insert)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, range(2)))
        finally:
            event.remove(engine, "before_cursor_execute", synchronize_insert)
    else:
        results = [invoke(0), invoke(1)]
    assert sorted(result.status_code for result in results) == [200, 409]
    with Session(engine) as session:
        denials = list(
            session.scalars(
                select(WorkflowEvent).where(
                    WorkflowEvent.event_type == "policy.denied",
                    WorkflowEvent.idempotency_key == "regression-body-conflict",
                )
            )
        )
        assert len(denials) == 1
        assert denials[0].reason == "idempotency_key_reused_with_different_request"


def test_non_ascii_invalid_bearer_returns_401_with_challenge(api):
    with TestClient(api.app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/prototypes", headers={b"Authorization": b"Bearer \xe9"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("setting", ["HUMAN_API_TOKEN", "WORKER_API_TOKEN"])
def test_non_ascii_configured_token_fails_closed(api, monkeypatch, setting):
    monkeypatch.setenv(setting, "non-ascii-" + "\u00e9" * 32)
    with TestClient(api.app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/prototypes", headers=headers())
    assert response.status_code == 503
    assert "non-ascii-" not in response.text


def test_concurrent_execute_allocates_one_sandbox(api, seed, engine):
    adapters = fake_adapters()
    allocation_lock = Lock()
    allocated = []
    original_create = adapters.sandbox.create

    def count_create(repository, commit, run_id):
        with allocation_lock:
            allocated.append(run_id)
        return original_create(repository, commit, run_id)

    adapters.sandbox.create = count_create
    api.app.state.adapters = adapters
    proposal, lease = claimed(api, seed)
    barrier = Barrier(2)

    def invoke(index):
        barrier.wait(timeout=10)
        return post(
            api,
            f"runs/{proposal['run_id']}/execute",
            {"expected_version": lease["version"], "lease_token": lease["lease_token"]},
            "worker",
            f"execute-{index}",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(invoke, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert allocated == [proposal["run_id"]]
    with Session(engine) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkflowEvent)
                .where(
                    WorkflowEvent.aggregate_id == proposal["id"],
                    WorkflowEvent.event_type == "sandbox.ready",
                )
            )
            == 1
        )
