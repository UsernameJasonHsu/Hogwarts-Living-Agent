"""M2.4.4 contracts written before implementation; real DB/Git, fake workload."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from hogwarts.auth import Principal
from hogwarts.models import BudgetReservation
from hogwarts.operations.contracts import Capacity, QuotaProfile, VersionedRetentionPolicy
from hogwarts.operations.quota import AggregateResourceQuotaManager
from hogwarts.operations.storage import StorageUsageAccounting
from hogwarts.service import DomainError
from pydantic import ValidationError
from sqlalchemy import select
from test_verification import HUMAN
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def quota(v, *, limit=2, storage=None, backend=None):
    w = v[0]
    return AggregateResourceQuotaManager(
        w["manager"],
        {
            HUMAN.owner_id: Capacity(
                slots=limit, cpu_millis=4000, memory_bytes=2**30, pids=512, storage_bytes=2**29
            )
        },
        (
            QuotaProfile(
                name="prototype",
                demand=Capacity(
                    slots=1, cpu_millis=1000, memory_bytes=2**28, pids=64, storage_bytes=2**20
                ),
                runtime_profile="capture",
                qa_profile="acceptance",
            ),
        ),
        storage
        or StorageUsageAccounting({HUMAN.owner_id: (v[1].store.objects.root,)}, min_free_bytes=0),
        backend=backend,
    )


@pytest.mark.parametrize(
    "model,kwargs",
    [
        (Capacity, {"slots": -1}),
        (Capacity, {"shell": "true"}),
        (VersionedRetentionPolicy, {"name": "bad", "schema_version": 2}),
        (VersionedRetentionPolicy, {"name": "bad", "host_path": "/"}),
    ],
)
def test_operation_contract_rejects_untrusted_configuration(model, kwargs):
    with pytest.raises(ValidationError):
        model(**kwargs)


def test_quota_reserve_idempotent_and_not_money(verification):
    v = verification
    q = quota(v)
    q.initialize(HUMAN, v[0]["run"])
    w = v[0]
    a = q.reserve(w["actor"], w["run"], "first", w["token"], "prototype")
    assert a == q.reserve(w["actor"], w["run"], "first", w["token"], "prototype")
    with w["factory"]() as s:
        b = s.scalar(select(BudgetReservation))
        assert b.status == "RESERVED" and b.actual_cents is None


def test_concurrent_reservation_does_not_oversubscribe(verification):
    v = verification
    q = quota(v, limit=1)
    q.initialize(HUMAN, v[0]["run"])
    w = v[0]
    barrier = Barrier(2)

    def reserve(key):
        barrier.wait()
        try:
            return q.reserve(w["actor"], w["run"], key, w["token"], "prototype")
        except DomainError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, ("one", "two")))
    assert sum(not isinstance(x, str) for x in results) == 1
    assert "quota_capacity_exceeded" in results


@pytest.mark.parametrize("bad", ["worker", "owner", "token", "profile", "grant"])
def test_quota_authorization(verification, bad):
    v = verification
    q = quota(v)
    w = v[0]
    q.initialize(HUMAN, w["run"])
    actor, token, profile = w["actor"], w["token"], "prototype"
    if bad == "worker":
        actor = Principal("other", HUMAN.owner_id, "worker", "engineer")
    elif bad == "owner":
        actor = Principal("other", "other", "human")
    elif bad == "token":
        token = "wrong"
    elif bad == "profile":
        profile = "not-approved"
    else:
        w["manager"].grants = {}
    with pytest.raises(DomainError):
        q.reserve(actor, w["run"], "denied", token, profile)


@pytest.mark.parametrize("dimension", list(Capacity.model_fields))
def test_each_aggregate_dimension_blocks_admission(verification, dimension):
    v = verification
    q = quota(v)
    profile = q.profiles["prototype"]
    limits = q.limits[HUMAN.owner_id].model_dump()
    limits[dimension] = profile.demand.model_dump()[dimension] - 1
    if limits[dimension] == 0:
        q = quota(v, limit=1)
        q.initialize(HUMAN, v[0]["run"])
        q.reserve(v[0]["actor"], v[0]["run"], "held", v[0]["token"], "prototype")
    else:
        q = AggregateResourceQuotaManager(
            v[0]["manager"], {HUMAN.owner_id: Capacity(**limits)}, (profile,), q.storage
        )
        q.initialize(HUMAN, v[0]["run"])
    with pytest.raises(DomainError, match="quota_capacity_exceeded"):
        q.reserve(v[0]["actor"], v[0]["run"], "over", v[0]["token"], "prototype")


def test_restart_and_same_key_concurrency(verification):
    v = verification
    q = quota(v)
    w = v[0]
    q.initialize(HUMAN, w["run"])

    def reserve(_):
        return quota(v).reserve(w["actor"], w["run"], "same", w["token"], "prototype")

    with ThreadPoolExecutor(2) as pool:
        a, b = list(pool.map(reserve, range(2)))
    assert a == b
    assert quota(v).receipt(w["actor"], w["run"], "same", w["token"]) == a


def test_operational_controller_exact_receipt_no_reexecution(verification):
    from hogwarts.operations.controller import OperationalPrototypeController

    v = verification
    w = v[0]
    q = quota(v)
    q.initialize(HUMAN, w["run"])
    controller = OperationalPrototypeController(q, v[1])
    result = controller.run(w["actor"], w["run"], "admitted", w["token"], "prototype")
    assert result == controller.run(w["actor"], w["run"], "admitted", w["token"], "prototype")
    assert v[3].calls.count("execute") == 1


def test_unknown_admission_intent_never_replays(verification, monkeypatch):
    from hogwarts.operations.controller import OperationalPrototypeController

    v = verification
    w = v[0]
    q = quota(v)
    q.initialize(HUMAN, w["run"])
    controller = OperationalPrototypeController(q, v[1])

    def failed(*_):
        raise OSError("injected controller crash")

    monkeypatch.setattr(v[1], "verify", failed)
    with pytest.raises(DomainError, match="quota_execution_uncertain"):
        controller.run(w["actor"], w["run"], "unknown", w["token"], "prototype")
    with pytest.raises(DomainError, match="quota_execution_uncertain"):
        controller.run(w["actor"], w["run"], "unknown", w["token"], "prototype")
    assert q.receipt(w["actor"], w["run"], "unknown", w["token"]).state == "UNKNOWN"
    assert not v[3].calls


def test_release_requires_absence_and_keeps_money(verification):
    from test_operations_retention import expire, gc

    v = verification
    w = v[0]
    q = quota(v)
    q.initialize(HUMAN, w["run"])
    r = q.reserve(w["actor"], w["run"], "allocation", w["token"], "prototype")
    w["manager"].create(w["actor"], w["run"], w["token"])
    expire(v)
    with pytest.raises(DomainError, match="quota_resources_not_absent"):
        q.release(HUMAN, w["run"], r.reservation_id, 2)
    g = gc(v)
    p = g.plan(HUMAN, w["run"], "release")
    a = g.approve(HUMAN, w["run"], p.plan_id, p.version)
    assert g.execute(HUMAN, w["run"], p.plan_id, a.version).state == "COMPLETE"
    released = q.release(HUMAN, w["run"], r.reservation_id, 2)
    assert released.state == "RELEASED"
    assert released == q.release(HUMAN, w["run"], r.reservation_id, 2)
    with w["factory"]() as s:
        b = s.scalar(select(BudgetReservation))
        assert b.status == "RESERVED" and b.actual_cents is None


def test_db_unavailable_is_not_success(verification, monkeypatch):
    from sqlalchemy.exc import OperationalError

    v = verification
    q = quota(v)

    class Down:
        def begin(self):
            raise OperationalError("injected", {}, Exception("database unavailable"))

    monkeypatch.setattr(q, "factory", Down())
    with pytest.raises(DomainError, match="operations_database_and_audit_unavailable"):
        q.initialize(HUMAN, v[0]["run"])


def test_multiple_runs_share_owner_capacity(verification):
    from datetime import timedelta
    from uuid import uuid4

    from hogwarts.models import Proposal, PrototypeRun, now

    v = verification
    q = quota(v, limit=1)
    w = v[0]
    q.initialize(HUMAN, w["run"])
    q.reserve(w["actor"], w["run"], "first-run", w["token"], "prototype")
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
        run = PrototypeRun(
            proposal_id=other.id,
            status="CLAIMED",
            worker_id=w["actor"].id,
            lease_token=str(uuid4()),
            lease_generation=1,
            lease_issued_generation=1,
            lease_version=1,
            lease_expires_at=now() + timedelta(minutes=5),
        )
        s.add(run)
        s.flush()
        identifier, token = run.id, run.lease_token
    with pytest.raises(DomainError, match="quota_capacity_exceeded"):
        q.reserve(w["actor"], identifier, "second-run", token, "prototype")
    with pytest.raises(DomainError, match="quota_request_conflict"):
        q.reserve(w["actor"], identifier, "first-run", token, "prototype")
