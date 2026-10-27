"""Contracts defined before implementation; fake daemon is never Linux evidence."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from hogwarts.auth import Principal
from hogwarts.models import GitWorkspace, IdempotencyRecord, PrototypeRun, WorkflowEvent, now
from hogwarts.reconciliation.contracts import CleanupJournal, Resource
from hogwarts.reconciliation.planner import ResourceReconciler
from hogwarts.service import DomainError, digest
from pydantic import ValidationError
from sqlalchemy import select
from test_workspaces import create, git
from test_workspaces import workspace as workspace

HUMAN = Principal("human:local-owner", "local-owner", "human")


def expire(w):
    with w["factory"].begin() as s:
        r = s.get(PrototypeRun, w["run"])
        r.lease_expires_at = now() - timedelta(seconds=1)


class FakeBackend:
    def __init__(self):
        self.items = [
            Resource(
                key="fake:container",
                kind="container",
                identifier="a" * 64,
                fingerprint=digest("owned"),
                disposition="CLEANUP_ELIGIBLE",
                reason="identity_confirmed",
            )
        ]
        self.removed = []
        self.fail = False

    def inventory(self, s, run, ws):
        if self.fail:
            raise DomainError(503, "docker_unavailable")
        return list(self.items)

    def remove(self, resource):
        if self.fail:
            raise DomainError(503, "docker_unavailable")
        self.removed.append(resource.identifier)
        self.items = [x for x in self.items if x.key != resource.key]


@pytest.fixture
def reconciler(workspace):
    w = workspace
    create(w)
    b = FakeBackend()
    return w, ResourceReconciler(w["manager"], backend=b), b


def test_schema_denies_host_paths_and_shell():
    with pytest.raises(ValidationError):
        Resource(
            key="x",
            kind="unknown",
            identifier="x",
            fingerprint="a" * 64,
            disposition="QUARANTINE",
            reason="unknown",
            host_path="/",
            shell="rm -rf /",
        )


def test_docker_semantic_fingerprint_preserves_command_order():
    from copy import deepcopy

    from hogwarts.reconciliation.docker import DockerResourceBackend

    f = DockerResourceBackend._container_fingerprint
    info = {
        "Id": "a" * 64,
        "Created": "fixed",
        "Name": "/owned",
        "Image": "pinned",
        "Config": {"Env": ["A=1", "B=2"], "Cmd": ["one", "two"]},
        "HostConfig": {"MaskedPaths": ["/b", "/a"]},
        "Mounts": [
            {"Destination": "/workspace", "Name": "input"},
            {"Destination": "/output", "Name": "output"},
        ],
    }
    other = deepcopy(info)
    other["Config"]["Env"].reverse()
    other["Mounts"].reverse()
    other["HostConfig"]["MaskedPaths"].reverse()
    assert f(info) == f(other)
    other["Config"]["Cmd"].reverse()
    assert f(info) != f(other)
    other["Config"]["Env"].append("A=3")
    with pytest.raises(DomainError):
        f(other)


@pytest.mark.parametrize("terminal", [False, True])
def test_dry_run_active_keeps_everything(reconciler, terminal):
    w, r, b = reconciler
    if terminal:
        with w["factory"].begin() as s:
            s.get(PrototypeRun, w["run"]).status = "READY_FOR_HUMAN_REVIEW"
    p = r.plan(HUMAN, w["run"], "dry")
    assert p.state == "PLANNED" and not b.removed
    assert not [x for x in p.resources if x.disposition == "QUARANTINE"], p.resources
    assert all(x.disposition == "KEEP" for x in p.resources if x.kind != "unknown")
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "COMPLETE" and not b.removed
    assert (w["root"] / next(x.identifier for x in p.resources if x.kind == "worktree")).exists()


def test_exact_cleanup_and_historical_retry(reconciler):
    w, r, b = reconciler
    expire(w)
    with w["factory"]() as s:
        ws = s.scalar(select(GitWorkspace))
    (w["root"] / ws.id / "src/main.txt").write_bytes(b"ordinary modified prototype\n")
    source = git(w["repo"], "status", "--porcelain")
    p = r.plan(HUMAN, w["run"], "cleanup")
    assert p == r.plan(HUMAN, w["run"], "cleanup")
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "COMPLETE" and len(b.removed) == 1
    assert r.execute(HUMAN, w["run"], p.plan_id, p.version) == q
    assert git(w["repo"], "status", "--porcelain") == source
    with w["factory"]() as s:
        assert s.scalar(select(GitWorkspace)).state == "CLEANED"
        assert s.scalar(select(WorkflowEvent).where(WorkflowEvent.event_type == "cleanup.complete"))


@pytest.mark.parametrize("change", ["generation", "version", "replacement", "unknown", "root"])
def test_changed_observation_fails_closed(reconciler, change):
    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "before")
    if change in {"generation", "version"}:
        with w["factory"].begin() as s:
            run = s.get(PrototypeRun, w["run"])
            if change == "generation":
                run.lease_generation += 1
            else:
                run.lease_version += 1
    elif change == "replacement":
        b.items[0] = b.items[0].model_copy(update={"fingerprint": digest("replacement")})
    elif change == "unknown":
        b.items[0] = b.items[0].model_copy(update={"disposition": "QUARANTINE"})
    else:
        (w["root"] / "foreign").mkdir()
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "QUARANTINED" and not b.removed


@pytest.mark.parametrize(
    "actor",
    [Principal("bad", "foreign", "human"), Principal("worker:engineer", "local-owner", "worker")],
)
def test_owner_human_only(reconciler, actor):
    w, r, b = reconciler
    with pytest.raises(DomainError):
        r.plan(actor, w["run"], "denied")
    assert not b.removed


def test_cas_and_concurrent_execution(reconciler):
    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "race")
    with pytest.raises(DomainError):
        r.execute(HUMAN, w["run"], p.plan_id, 999)
    barrier = Barrier(2)

    def execute():
        barrier.wait()
        try:
            return r.execute(HUMAN, w["run"], p.plan_id, p.version).state
        except DomainError:
            return "BUSY"

    with ThreadPoolExecutor(2) as pool:
        states = list(pool.map(lambda _: execute(), range(2)))
    assert "COMPLETE" in states and len(b.removed) == 1


def test_restart_never_replays_unknown(reconciler):
    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "restart")
    with w["factory"].begin() as s:
        row = s.get(IdempotencyRecord, p.plan_id)
        row.response = {**row.response, "state": "EXECUTING", "version": 2}
        row.request_hash = digest([w["run"], row.key, 2])
    fresh = ResourceReconciler(w["manager"], backend=b)
    q = fresh.execute(HUMAN, w["run"], p.plan_id, 2)
    assert q.state == "UNKNOWN" and not b.removed


def test_daemon_failure_no_success(reconciler):
    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "failure")
    b.fail = True
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "UNKNOWN" and not b.removed


def test_permanent_evidence_and_unknown_root_retained(reconciler):
    w, r, b = reconciler
    expire(w)
    unknown = w["root"] / "unowned"
    unknown.mkdir()
    (unknown / "sentinel").write_bytes(b"keep")
    p = r.plan(HUMAN, w["run"], "unknown-root")
    assert any(x.disposition == "QUARANTINE" for x in p.resources)
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "QUARANTINED" and (unknown / "sentinel").read_bytes() == b"keep"
    assert not b.removed


@pytest.mark.parametrize("mode", ["hardlink", "traversal", "changed_file", "missing_registration"])
def test_worktree_identity_attacks(reconciler, mode):
    w, r, b = reconciler
    expire(w)
    with w["factory"]() as s:
        ws = s.scalar(select(GitWorkspace))
    path = w["root"] / ws.id
    p = r.plan(HUMAN, w["run"], "tree-attack")
    if mode == "hardlink":
        os.link(path / "src/main.txt", path / "src/linked.txt")
    elif mode == "changed_file":
        (path / "src/main.txt").write_bytes(b"changed after plan")
    elif mode == "traversal":
        with w["factory"].begin() as s:
            s.get(GitWorkspace, ws.id).repository_path = str(w["repo"].parent)
    else:
        original = w["repo"] / ".git/worktrees" / ws.id / "commondir"
        saved = original.read_bytes()
        original.write_bytes(b"../../foreign")
    try:
        q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
        assert q.state in {"QUARANTINED", "UNKNOWN"} and not b.removed
        assert path.exists()
    finally:
        if mode == "hardlink":
            (path / "src/linked.txt").unlink()
        elif mode == "missing_registration":
            original.write_bytes(saved)
        elif mode == "traversal":
            with w["factory"].begin() as s:
                s.get(GitWorkspace, ws.id).repository_path = str(w["repo"])


@pytest.mark.parametrize("mode", ["revoked", "cancelled", "missing_issuance", "missing_expiry"])
def test_lease_lifecycle(reconciler, mode):
    w, r, b = reconciler
    with w["factory"].begin() as s:
        run = s.get(PrototypeRun, w["run"])
        if mode in {"revoked", "cancelled"}:
            run.lease_generation += 1
            run.lease_version += 1
            run.lease_revoked_at = now()
            if mode == "cancelled":
                run.status = "CANCELLED"
        elif mode == "missing_issuance":
            run.lease_issued_generation = 0
        else:
            run.lease_expires_at = None
    p = r.plan(HUMAN, w["run"], "lease")
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == ("COMPLETE" if mode in {"revoked", "cancelled"} else "QUARANTINED")
    assert bool(b.removed) == (mode in {"revoked", "cancelled"})


def test_db_intent_failure_has_no_effect(reconciler, monkeypatch):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "db-fail")

    def failed(s):
        if any(isinstance(x, WorkflowEvent) and x.event_type == "cleanup.executing" for x in s.new):
            raise OperationalError("injected", {}, Exception("injected"))

    event.listen(Session, "before_commit", failed)
    try:
        with pytest.raises(DomainError) as e:
            r.execute(HUMAN, w["run"], p.plan_id, p.version)
        assert e.value.status == 503 and not b.removed
    finally:
        event.remove(Session, "before_commit", failed)
    assert r.execute(HUMAN, w["run"], p.plan_id, p.version).state == "COMPLETE"


def test_final_commit_failure_requires_explicit_new_plan(reconciler):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "commit-fail")

    def failed(s):
        if any(isinstance(x, WorkflowEvent) and x.event_type == "cleanup.complete" for x in s.new):
            raise OperationalError("injected", {}, Exception("injected"))

    event.listen(Session, "before_commit", failed)
    try:
        with pytest.raises(DomainError):
            r.execute(HUMAN, w["run"], p.plan_id, p.version)
    finally:
        event.remove(Session, "before_commit", failed)
    assert len(b.removed) == 1
    q = r.execute(HUMAN, w["run"], p.plan_id, 2)
    assert q.state == "UNKNOWN" and len(b.removed) == 1
    fresh = r.plan(HUMAN, w["run"], "explicit-new-plan")
    assert r.execute(HUMAN, w["run"], fresh.plan_id, fresh.version).state == "COMPLETE"


def test_immediate_reinspect_blocks_replacement(reconciler, monkeypatch):
    w, r, b = reconciler
    expire(w)
    p = r.plan(HUMAN, w["run"], "toctou")
    original = b.inventory
    count = 0

    def changed(*args):
        nonlocal count
        count += 1
        if count == 2:
            b.items[0] = b.items[0].model_copy(update={"fingerprint": digest("replaced")})
        return original(*args)

    monkeypatch.setattr(b, "inventory", changed)
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "UNKNOWN" and not b.removed


def test_partial_effect_never_replayed(reconciler, monkeypatch):
    w, r, b = reconciler
    expire(w)
    b.items.append(b.items[0].model_copy(update={"key": "fake:second", "identifier": "b" * 64}))
    p = r.plan(HUMAN, w["run"], "partial")
    original = b.remove

    def failed(resource):
        if b.removed:
            raise DomainError(503, "docker_operation_failed")
        original(resource)

    monkeypatch.setattr(b, "remove", failed)
    q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
    assert q.state == "UNKNOWN" and len(q.completed) == len(b.removed) == 1
    assert r.execute(HUMAN, w["run"], p.plan_id, q.version) == q
    assert len(b.removed) == 1


def test_persisted_schema_and_no_credentials(reconciler):
    import json
    from pathlib import Path

    import jsonschema

    w, r, b = reconciler
    p = r.plan(HUMAN, w["run"], "schema")
    schema = json.loads(Path("contracts/cleanup-journal.v1.schema.json").read_text(encoding="utf8"))
    jsonschema.validate(p.model_dump(mode="json"), schema)
    assert w["token"] not in p.model_dump_json()
    assert CleanupJournal.model_validate_json(p.model_dump_json()) == p


def test_concurrent_plans_share_one_intent(reconciler):
    w, r, b = reconciler
    barrier = Barrier(2)

    def plan():
        barrier.wait()
        return r.plan(HUMAN, w["run"], "concurrent-plan")

    with ThreadPoolExecutor(2) as pool:
        plans = list(pool.map(lambda _: plan(), range(2)))
    assert plans[0] == plans[1] and not b.removed


def test_cross_run_plan_rejected(reconciler):
    from uuid import uuid4

    w, r, b = reconciler
    p = r.plan(HUMAN, w["run"], "correct-run")
    with pytest.raises(DomainError):
        r.execute(HUMAN, str(uuid4()), p.plan_id, p.version)
    assert not b.removed


def test_descriptor_replacement_rejected_before_read(tmp_path, monkeypatch):
    import hogwarts.reconciliation.inventory as module
    from hogwarts.reconciliation.inventory import bounded_hash

    first, other = tmp_path / "first", tmp_path / "other"
    first.write_bytes(b"ordinary")
    other.write_bytes(b"do not read substituted bytes")
    expected = first.stat()
    original = os.open

    def replaced(path, flags, *args, **kwargs):
        if path == first:
            first.unlink()
            os.link(other, first)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(module.os, "open", replaced)
    with pytest.raises(DomainError):
        bounded_hash(first, expected)
