"""Fault, restart and concurrency evidence; all resources are disposable fixtures."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from hogwarts.auth import Principal
from hogwarts.models import ArtifactMetadata, IdempotencyRecord, WorkflowEvent
from hogwarts.operations.contracts import EvidenceHold
from hogwarts.operations.controller import OperationalPrototypeController
from hogwarts.operations.health import OperationalHealth
from hogwarts.operations.storage import StorageUsageAccounting
from hogwarts.reconciliation.contracts import Resource
from hogwarts.service import DomainError, digest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_operations import quota
from test_operations_retention import EmptyBackend, expire, gc, prepared
from test_verification import HUMAN, review, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def test_reservation_commit_rollback_and_retry(verification):
    v = verification
    w = v[0]
    q = quota(v)
    q.initialize(HUMAN, w["run"])
    fired = []

    def fail(s):
        if not fired and any(
            isinstance(x, WorkflowEvent) and x.event_type == "operations.quota_reserved"
            for x in s.new
        ):
            fired.append(True)
            raise OperationalError("injected", {}, Exception("commit failed"))

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(DomainError):
            q.reserve(w["actor"], w["run"], "commit", w["token"], "prototype")
    finally:
        event.remove(Session, "before_commit", fail)
    assert fired
    result = quota(v).reserve(w["actor"], w["run"], "commit", w["token"], "prototype")
    assert result.state == "RESERVED"
    with w["factory"]() as s:
        assert len(q.ledger(s, HUMAN, lock=False)[1].reservations) == 1


def test_gc_final_commit_failure_restart_never_repeats(verification):
    v = verification
    path = prepared(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "commit")
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    fired = []

    def fail(s):
        if not fired and any(
            isinstance(x, WorkflowEvent) and x.event_type == "operations.gc_complete" for x in s.new
        ):
            fired.append(True)
            raise OperationalError("injected", {}, Exception("commit failed"))

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(DomainError):
            g.execute(HUMAN, v[0]["run"], p.plan_id, a.version)
    finally:
        event.remove(Session, "before_commit", fail)
    assert fired and not path.exists()
    with v[0]["factory"]() as s:
        version = s.get(IdempotencyRecord, p.plan_id).response["version"]
    result = gc(v).execute(HUMAN, v[0]["run"], p.plan_id, version)
    assert result.state == "UNKNOWN" and not path.exists()


def test_in_use_restart_never_calls_workload(verification, monkeypatch):
    v = verification
    w = v[0]
    q = quota(v)
    q.initialize(HUMAN, w["run"])
    r = q.reserve(w["actor"], w["run"], "started", w["token"], "prototype")
    q._transition(w["actor"], w["run"], r.reservation_id, w["token"], "RESERVED", "IN_USE")
    calls = []
    monkeypatch.setattr(v[1], "verify", lambda *args: calls.append(args))
    with pytest.raises(DomainError, match="quota_execution_uncertain"):
        OperationalPrototypeController(quota(v), v[1]).run(
            w["actor"], w["run"], "started", w["token"], "prototype"
        )
    assert not calls
    result = OperationalHealth(q).snapshot(HUMAN, w["run"])
    assert result["held_capacity"]["slots"] == 1 and result["status"] == "blocked"


@pytest.mark.parametrize("bad", ["worker", "owner", "version", "cross-run"])
def test_gc_admin_and_plan_bindings(verification, bad):
    v = verification
    path = prepared(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "binding")
    actor, run, version = HUMAN, v[0]["run"], p.version
    if bad == "worker":
        actor = v[0]["actor"]
    elif bad == "owner":
        actor = Principal("other", "other", "human")
    elif bad == "version":
        version = True
    else:
        from uuid import uuid4

        run = str(uuid4())
    with pytest.raises(DomainError):
        g.approve(actor, run, p.plan_id, version)
    assert path.exists()


def test_concurrent_gc_exactly_once(verification):
    v = verification
    path = prepared(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "concurrent")
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    barrier = Barrier(2)

    def execute(_):
        barrier.wait()
        try:
            return gc(v).execute(HUMAN, v[0]["run"], p.plan_id, a.version).state
        except DomainError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(execute, range(2)))
    assert "COMPLETE" in result and not path.exists()
    assert all(x in {"COMPLETE", "retention_cas_conflict"} for x in result)


def test_guard_rechecks_hold_before_second_delete(verification, monkeypatch):
    v = verification
    path = prepared(v)

    class Resources(EmptyBackend):
        def __init__(self):
            self.items = [
                Resource(
                    key="container:fixture",
                    kind="container",
                    identifier="a" * 64,
                    fingerprint=digest("container"),
                    disposition="CLEANUP_ELIGIBLE",
                    reason="fixture",
                ),
                Resource(
                    key="volume:fixture",
                    kind="volume",
                    identifier="disposable-volume-fixture",
                    fingerprint=digest("volume"),
                    disposition="CLEANUP_ELIGIBLE",
                    reason="fixture",
                ),
            ]
            self.removed = []

        def inventory(self, s, run, ws):
            return list(self.items)

        def remove(self, target):
            self.removed.append(target.key)
            self.items.remove(target)

    backend = Resources()
    g = gc(v, backend=backend)
    p = g.plan(HUMAN, v[0]["run"], "per-target")
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    original = g.gate
    count = []

    def gate(s, actor, run, identifier):
        count.append(True)
        if len(count) == 3:
            # Fault injection uses the already Run-locked session. Real human
            # Hold writers serialize on the same lock, rather than nesting DBs.
            value = EvidenceHold(
                run_id=run, owner_id=actor.owner_id, active=True, reason="incident"
            )
            g.references.save(s, actor, "evidence-hold.v1:" + run, value)
            s.flush()
        return original(s, actor, run, identifier)

    monkeypatch.setattr(g, "gate", gate)
    result = g.execute(HUMAN, v[0]["run"], p.plan_id, a.version)
    assert result.state == "UNKNOWN" and backend.removed == ["container:fixture"]
    assert path.exists()


@pytest.mark.parametrize("failure", ["missing-verifier", "tampering", "io"])
def test_evidence_uncertainty_blocks_gc_without_mutation(verification, monkeypatch, failure):
    v = verification
    info = verify(v)
    review(v, info, decision="REJECTED")
    expire(v)
    g = gc(v)
    if failure == "missing-verifier":
        g.references.store = None
    else:

        def bad(*args):
            if failure == "io":
                raise OSError("injected storage unavailable")
            from hogwarts.artifacts.contracts import IntegrityError

            raise IntegrityError(409, "artifact_integrity_failed")

        monkeypatch.setattr(g.references.store.objects, "read", bad)
    p = g.plan(HUMAN, v[0]["run"], "evidence-unknown")
    assert not p.eligible
    with v[0]["factory"]() as s:
        assert all(x.state == "READY" for x in s.scalars(select(ArtifactMetadata)))


def test_health_dependency_failure_is_unknown_not_zero(verification, monkeypatch):
    v = verification
    q = quota(v)
    q.initialize(HUMAN, v[0]["run"])

    def down(*_):
        raise OSError("injected private filesystem unavailable")

    monkeypatch.setattr(q.storage, "measure", down)
    result = OperationalHealth(q).snapshot(HUMAN, v[0]["run"])
    assert (
        result["status"] == "unknown" and "held_capacity" not in result and not result["m3_ready"]
    )


def test_storage_unmaterialized_artifacts_still_charge(verification, monkeypatch):
    v = verification
    verify(v)
    storage = quota(v).storage
    original = storage.tree

    def hidden(root):
        tree, _ = original(root)
        return {"": tree[""]}, 0

    monkeypatch.setattr(storage, "tree", hidden)
    with v[0]["factory"]() as s:
        usage = storage.measure(s, HUMAN.owner_id)
    assert usage.logical_bytes == 0
    assert usage.unmaterialized_artifact_bytes == usage.declared_artifact_bytes > 0


def test_source_repository_cannot_be_storage_scope(verification):
    v = verification
    storage = StorageUsageAccounting({HUMAN.owner_id: (v[0]["repo"],)})
    with pytest.raises(DomainError, match="storage_source_repository_forbidden"):
        quota(v, storage=storage)


@pytest.mark.parametrize("binding", ["owner", "producer", "lease"])
def test_foreign_artifact_binding_never_reads_bytes(verification, monkeypatch, binding):
    v = verification
    info = verify(v)
    review(v, info, decision="REJECTED")
    expire(v)
    with v[0]["factory"].begin() as s:
        row = s.scalar(select(ArtifactMetadata))
        identifier = row.id
        if binding == "owner":
            row.owner_id = "foreign-owner"
        elif binding == "producer":
            row.producer_id = "foreign-worker"
        else:
            row.lease_hash = "0" * 64
    original = v[1].store.objects.read
    calls = []

    def read(identifier, *args):
        calls.append(identifier)
        return original(identifier, *args)

    monkeypatch.setattr(v[1].store.objects, "read", read)
    result = gc(v).plan(HUMAN, v[0]["run"], "foreign")
    assert not result.eligible and identifier not in calls


@pytest.mark.parametrize("change", ["generation", "version", "initialization"])
def test_quota_admin_and_fencing_conflicts(verification, change):
    from hogwarts.models import PrototypeRun

    v = verification
    w = v[0]
    q = quota(v)
    if change == "initialization":
        with pytest.raises(DomainError, match="operations_owner_human_required"):
            q.initialize(w["actor"], w["run"])
        return
    q.initialize(HUMAN, w["run"])
    r = q.reserve(w["actor"], w["run"], "fenced", w["token"], "prototype")
    if change == "generation":
        with w["factory"].begin() as s:
            row = s.get(PrototypeRun, w["run"])
            row.lease_generation += 1
            row.lease_version += 1
        with pytest.raises(DomainError):
            q._transition(w["actor"], w["run"], r.reservation_id, w["token"], "RESERVED", "IN_USE")
    else:
        with pytest.raises(DomainError, match="quota_cas_conflict"):
            q.release(HUMAN, w["run"], r.reservation_id, 1)


def test_docker_unavailable_denies_admission_and_gc(verification):
    v = verification

    class Down:
        def inventory(self, *args):
            raise OSError("injected daemon unavailable")

    q = quota(v, backend=Down())
    q.initialize(HUMAN, v[0]["run"])
    with pytest.raises(DomainError, match="operations_state_unavailable"):
        q.reserve(v[0]["actor"], v[0]["run"], "down", v[0]["token"], "prototype")
    prepared(v)
    with pytest.raises(DomainError):
        gc(v, backend=Down()).plan(HUMAN, v[0]["run"], "down")


def test_concurrent_different_runs_share_owner_lock(verification):
    from datetime import timedelta
    from uuid import uuid4

    from hogwarts.models import Proposal, PrototypeRun, now

    v = verification
    w = v[0]
    q = quota(v, limit=1)
    q.initialize(HUMAN, w["run"])
    with w["factory"].begin() as s:
        p = s.get(Proposal, w["proposal"])
        other = Proposal(
            owner_id=p.owner_id,
            project_id=p.project_id,
            author_agent_id=p.author_agent_id,
            status="QUEUED",
            document=p.document,
        )
        s.add(other)
        s.flush()
        r = PrototypeRun(
            proposal_id=other.id,
            worker_id=w["actor"].id,
            status="CLAIMED",
            lease_token=str(uuid4()),
            lease_generation=1,
            lease_issued_generation=1,
            lease_version=1,
            lease_expires_at=now() + timedelta(minutes=5),
        )
        s.add(r)
        s.flush()
        other_id, other_token = r.id, r.lease_token
    barrier = Barrier(2)

    def reserve(args):
        identifier, token, key = args
        barrier.wait()
        try:
            return quota(v, limit=1).reserve(w["actor"], identifier, key, token, "prototype").state
        except DomainError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        result = list(
            pool.map(
                reserve, [(w["run"], w["token"], "run-one"), (other_id, other_token, "run-two")]
            )
        )
    assert sorted(result) == ["RESERVED", "quota_capacity_exceeded"]


def test_versioned_operations_schemas_match_source():
    import json
    from pathlib import Path

    from hogwarts.operations.contracts import (
        EvidenceHold,
        GCJournal,
        QuotaLedger,
        StorageUsage,
        VersionedRetentionPolicy,
    )

    for name, model in (
        ("quota-ledger", QuotaLedger),
        ("retention-gc", GCJournal),
        ("evidence-hold", EvidenceHold),
        ("retention-policy", VersionedRetentionPolicy),
        ("storage-usage", StorageUsage),
    ):
        assert (
            json.loads(Path("contracts", name + ".v1.schema.json").read_text(encoding="utf8"))
            == model.model_json_schema()
        )


@pytest.mark.parametrize("field", ["demand", "profile"])
def test_corrupt_quota_commitment_cannot_reduce_accounting(verification, field):
    v = verification
    w = v[0]
    q = quota(v)
    q.initialize(HUMAN, w["run"])
    q.reserve(w["actor"], w["run"], "held", w["token"], "prototype")
    with w["factory"].begin() as s:
        row = s.scalar(select(IdempotencyRecord).where(IdempotencyRecord.key == "quota-ledger.v1"))
        response = dict(row.response)
        r = dict(response["reservations"][0])
        if field == "demand":
            r["demand"] = {**r["demand"], "slots": 0}
        else:
            r["profile_sha256"] = "0" * 64
        row.response = {**response, "reservations": [r]}
    with pytest.raises(DomainError, match="quota_ledger_state_invalid"):
        q.reserve(w["actor"], w["run"], "new", w["token"], "prototype")
