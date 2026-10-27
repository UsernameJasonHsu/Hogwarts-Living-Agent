from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from formal_identity import inprocess_service
from hogwarts.entrypoints.bootstrap import create_formal_app
from hogwarts.models import (
    BudgetReservation,
    IdempotencyRecord,
    Project,
    PrototypeRun,
    WorkflowEvent,
    WorkspaceBudget,
    now,
)
from hogwarts.service import DomainError
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_formal import gateway, version
from test_verification import HUMAN
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def run(g, w, key="execute"):
    return g.execute(w["actor"], w["run"], version(w), w["token"], key)


@pytest.mark.parametrize("mode", ["unknown", "operational"])
def test_formal_startup_never_implicitly_loads_runtime(mode):
    with pytest.raises(DomainError):
        create_formal_app(mode=mode)


@pytest.mark.parametrize("mode", ["disabled", "fake"])
def test_formal_bootstrap_explicit_side_effect_free_mode(verification, mode):
    v = verification
    w = v[0]
    app = create_formal_app(w["factory"].kw["bind"], mode=mode)
    with TestClient(app) as c:
        result = c.post(
            f"/api/v1/runs/{w['run']}/execute",
            json={"expected_version": version(w), "lease_token": w["token"]},
            headers=headers("worker", mode),
        )
    assert result.status_code == (503 if mode == "disabled" else 200), result.text
    assert not v[2].calls and not v[3].calls


@pytest.mark.parametrize("adapter", ["runtime", "sandbox", "artifacts", "tests"])
def test_formal_fake_rejects_injected_effect_adapter(verification, adapter):
    v = verification
    w = v[0]
    app = create_formal_app(w["factory"].kw["bind"], mode="fake")
    calls = []

    class Injected:
        def __getattr__(self, name):
            return lambda *args: calls.append(name)

    setattr(app.state.adapters, adapter, Injected())
    with TestClient(app) as c:
        result = c.post(
            f"/api/v1/runs/{w['run']}/execute",
            json={"expected_version": version(w), "lease_token": w["token"]},
            headers=headers("worker", "injected"),
        )
    assert (
        result.status_code == 403 and result.json()["detail"] == "formal_nonfake_adapter_forbidden"
    )
    assert not calls


@pytest.mark.parametrize(
    "bad", ["quota", "budget", "counter", "policy", "expired", "revoked", "grant"]
)
def test_formal_admission_denies_before_resource_effect(verification, bad):
    from hogwarts.leases.manager import DurableLeaseManager

    v = verification
    w = v[0]
    g = gateway(v, slots=1 if bad == "quota" else 2)
    if bad == "quota":
        with g.boundary.scope(w["actor"], w["run"], "admission"):
            g.quota.reserve(w["actor"], w["run"], "held", w["token"], "prototype")
    elif bad == "revoked":
        with w["factory"]() as s:
            gen = s.get(PrototypeRun, w["run"]).lease_generation
        DurableLeaseManager(g.factory).revoke(HUMAN, w["run"], gen, "revoke")
    elif bad == "grant":
        w["manager"].grants = {}
    else:
        with w["factory"].begin() as s:
            if bad == "budget":
                s.scalar(select(BudgetReservation)).actual_cents = 0
            elif bad == "counter":
                s.get(WorkspaceBudget, HUMAN.owner_id).reserved_cents = 0
            elif bad == "policy":
                s.get(Project, w["grant"].project_id).preapproved = False
            else:
                s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        run(g, w)
    assert not v[2].calls and not v[3].calls


def test_forged_scope_without_durable_dispatch_cannot_publish(verification):
    v = verification
    w = v[0]
    g = gateway(v)
    with pytest.raises(DomainError, match="formal_execution_capability_required"):
        with g.boundary.scope(w["actor"], w["run"], "execute"):
            pytest.fail("unsealed capability accepted")
    with g.boundary.scope(w["actor"], w["run"], "admission"):
        with pytest.raises(DomainError):
            with g.boundary.execution_scope(
                w["actor"], w["run"], w["token"], SimpleNamespace(reservation_id=str(uuid4()))
            ):
                pytest.fail("missing dispatch accepted")


@pytest.mark.parametrize("key_kind", ["same", "different"])
def test_concurrent_formal_admission_has_one_execution(verification, key_kind):
    v = verification
    w = v[0]
    g = gateway(v)
    expected = version(w)
    barrier = Barrier(2)

    def execute(i):
        barrier.wait()
        try:
            return g.execute(
                w["actor"],
                w["run"],
                expected,
                w["token"],
                "same" if key_kind == "same" else "key" + str(i),
            )
        except DomainError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(execute, range(2)))
    assert any(isinstance(x, dict) and x["status"] == "READY_FOR_HUMAN_REVIEW" for x in results), (
        results
    )
    assert v[3].calls.count("execute") == 1


def test_cancel_after_admission_retains_resource_and_money(verification, monkeypatch):
    v = verification
    w = v[0]
    g = gateway(v)
    app = inprocess_service(g)

    def cancel(*args, **kwargs):
        with TestClient(app) as c:
            result = c.post(
                f"/api/v1/prototypes/{w['proposal']}/cancel",
                json={"expected_version": version(w)},
                headers=headers("human", "cancel"),
            )
        assert result.status_code == 200, result.text
        raise DomainError(409, "injected_after_cancel")

    monkeypatch.setattr(v[1], "verify", cancel)
    with pytest.raises(DomainError):
        run(g, w)
    with w["factory"]() as s:
        budget = s.scalar(select(BudgetReservation))
        assert budget.status == "RESERVED" and budget.actual_cents is None
        ledger = g.quota.ledger(s, HUMAN, lock=False)[1]
        assert g.quota.total(ledger).slots == 1
        journal = s.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key.like("formal-dispatch.v1:%"))
        )
        assert journal.response["state"] == "UNKNOWN"
    assert not v[3].calls


def test_dispatch_commit_failure_has_no_external_effects(verification):
    v = verification
    w = v[0]
    g = gateway(v)
    fired = []

    def fail(s):
        if not fired and any(
            isinstance(x, WorkflowEvent) and x.event_type == "operations.formal_dispatching"
            for x in s.new
        ):
            fired.append(True)
            raise OperationalError("injected", {}, Exception("commit failed"))

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(DomainError):
            run(g, w)
    finally:
        event.remove(Session, "before_commit", fail)
    assert fired and not v[2].calls and not v[3].calls


def test_unknown_dispatch_never_replays(verification, monkeypatch):
    v = verification
    w = v[0]
    g = gateway(v)
    expected = version(w)

    def fail(actor, run_id, key, token, profile):
        r = g.quota.reserve(actor, run_id, key, token, profile)
        g.quota._transition(actor, run_id, r.reservation_id, token, "RESERVED", "IN_USE")
        raise OSError("injected process boundary failure")

    monkeypatch.setattr(g.operational, "run", fail)
    with pytest.raises(DomainError):
        g.execute(w["actor"], w["run"], expected, w["token"], "unknown")
    with pytest.raises(DomainError, match="formal_execution_uncertain"):
        g.execute(w["actor"], w["run"], expected, w["token"], "unknown")
    assert not v[3].calls


def test_escaped_context_has_no_effect_authority(verification, monkeypatch):
    v = verification
    w = v[0]
    g = gateway(v)
    saved = []
    original = v[1].verify

    def capture(*args, **kwargs):
        saved.append(copy_context())
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1], "verify", capture)
    result = run(g, w)
    assert result["verified"] is False
    with pytest.raises(DomainError, match="formal_effect_without_gate"):
        saved[0].run(v[1].store.objects.publish, str(uuid4()), b"no", "0" * 64)
    artifact = g.read_artifact(HUMAN, w["run"], result["bundle_artifact_id"])
    assert artifact.evidence.assessment.status == "unverified"


@pytest.mark.parametrize(
    "operation",
    [
        "docker_create",
        "docker_remove",
        "docker_prune",
        "git_push",
        "git_merge",
        "git_prune",
        "worktree_add",
        "publication",
    ],
)
def test_generic_dispatch_cannot_bypass_semantic_gate(verification, operation):
    v = verification
    w = v[0]
    g = gateway(v)
    calls = []
    driver = SimpleNamespace(call=lambda *a, **kw: calls.append(a))
    g.boundary.docker(driver)
    with g.boundary.scope(w["actor"], w["run"], "inspect"):
        with pytest.raises(DomainError):
            if operation.startswith("docker_"):
                driver.call(
                    "container",
                    {"docker_create": "create", "docker_remove": "rm", "docker_prune": "prune"}[
                        operation
                    ],
                    "untrusted",
                )
            elif operation == "publication":
                v[1].store.objects.publish("bad", b"bad", "0" * 64)
            else:
                args = {
                    "git_push": ("push",),
                    "git_merge": ("merge",),
                    "git_prune": ("worktree", "prune"),
                    "worktree_add": ("worktree", "add", "bad"),
                }[operation]
                w["manager"].git.run(w["repo"], *args)
    assert not calls and not v[2].calls and not v[3].calls


@pytest.mark.parametrize("change", ["expired", "generation", "revoked", "grant"])
def test_authority_change_after_admission_stops_before_first_effect(
    verification, monkeypatch, change
):
    v = verification
    w = v[0]
    g = gateway(v)
    original = v[1].verify

    def changed(*args, **kwargs):
        if change == "grant":
            w["manager"].grants = {}
        else:
            with w["factory"].begin() as s:
                r = s.get(PrototypeRun, w["run"])
                if change == "expired":
                    r.lease_expires_at = now() - timedelta(seconds=1)
                elif change == "generation":
                    r.lease_generation += 1
                else:
                    r.lease_revoked_at = now()
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1], "verify", changed)
    with pytest.raises(DomainError):
        run(g, w)
    assert not v[2].calls and not v[3].calls
    with w["factory"]() as s:
        assert g.quota.total(g.quota.ledger(s, HUMAN, lock=False)[1]).slots == 1
        assert s.scalar(select(BudgetReservation)).status == "RESERVED"


def test_unknown_qa_does_not_settle_budget_or_replay(verification, monkeypatch):
    v = verification
    w = v[0]
    g = gateway(v)
    expected = version(w)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise OSError("injected QA Docker transport uncertainty")

    monkeypatch.setattr(v[3], "execute", unavailable)
    result = g.execute(w["actor"], w["run"], expected, w["token"], "unknown-qa")
    assert result["status"] == "UNKNOWN" and result["verified"] is False
    with w["factory"]() as s:
        budget = s.scalar(select(BudgetReservation))
        assert budget.status == "RESERVED" and budget.actual_cents is None
        assert g.quota.total(g.quota.ledger(s, HUMAN, lock=False)[1]).slots == 1
    assert g.execute(w["actor"], w["run"], expected, w["token"], "unknown-qa") == result
    assert len(calls) == 1
