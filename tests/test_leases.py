"""M2.4.1 contracts; real DB/Git, explicit fake execution where stated."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from hogwarts.leases.contracts import Heartbeat
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import PrototypeRun, WorkflowEvent, now
from hogwarts.schemas import VersionCommand
from hogwarts.service import DomainError, cancel, command
from sqlalchemy import select
from test_artifacts import artifacts as artifacts
from test_sandboxes import sandbox as sandbox
from test_verification import HUMAN, review, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def heartbeat(w, **changes):
    args = dict(lease_token=w["token"], generation=1, expected_version=1)
    args.update(changes)
    return Heartbeat(**args)


def test_heartbeat_durable_idempotent_and_restart(workspace):
    w = workspace
    m = DurableLeaseManager(w["factory"])
    a = m.heartbeat(w["actor"], w["run"], heartbeat(w), "beat-1")
    assert a.generation == 1 and a.version == 2
    assert (
        DurableLeaseManager(w["factory"]).heartbeat(w["actor"], w["run"], heartbeat(w), "beat-1")
        == a
    )
    with w["factory"]() as s:
        run = s.get(PrototypeRun, w["run"])
        assert run.lease_heartbeat_at is not None
        assert run.lease_version == 2
    with pytest.raises(DomainError):
        m.heartbeat(w["actor"], w["run"], heartbeat(w, expected_version=2), "beat-1")


@pytest.mark.parametrize(
    "bad", ["generation", "version", "token", "worker", "owner", "expired", "revoked"]
)
def test_heartbeat_denials(workspace, bad):
    w = workspace
    m = DurableLeaseManager(w["factory"])
    a = w["actor"]
    changes = {}
    if bad == "generation":
        changes["generation"] = 0
    if bad == "version":
        changes["expected_version"] = 9
    if bad == "token":
        changes["lease_token"] = "wrong"
    if bad == "worker":
        a = replace(a, id="other")
    if bad == "owner":
        a = replace(a, owner_id="other")
    if bad == "expired":
        with w["factory"].begin() as s:
            s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
    if bad == "revoked":
        m.revoke(HUMAN, w["run"], 1, "revoke")
    with pytest.raises(DomainError):
        m.heartbeat(a, w["run"], heartbeat(w, **changes), "denied")


def test_concurrent_cas_has_one_winner(workspace):
    w = workspace
    m = DurableLeaseManager(w["factory"])

    def run(i):
        try:
            return m.heartbeat(w["actor"], w["run"], heartbeat(w), "beat-" + str(i))
        except DomainError as e:
            return e.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(not isinstance(x, str) for x in results) == 1


def test_revoke_stop_retry_and_cleanup(workspace):
    w = workspace
    m = DurableLeaseManager(w["factory"])
    ws = w["manager"].create(w["actor"], w["run"], w["token"])
    a = m.revoke(HUMAN, w["run"], 1, "revoke")
    assert a.generation == 2 and a.stop_state == "PENDING"
    assert m.revoke(HUMAN, w["run"], 1, "revoke") == a
    with pytest.raises(DomainError):
        w["manager"].export_diff(w["actor"], w["run"], w["token"])

    def fail():
        raise OSError("do not expose")

    assert m.stop(HUMAN, w["run"], fail).stop_state == "FAILED"
    calls = []
    assert m.stop(HUMAN, w["run"], lambda: calls.append(1)).stop_state == "CONFIRMED"
    m.stop(HUMAN, w["run"], lambda: calls.append(2))
    assert calls == [1]
    assert w["manager"].cleanup(w["actor"], w["run"], w["token"]).id == ws.id
    with w["factory"]() as s:
        events = list(
            s.scalars(select(WorkflowEvent).where(WorkflowEvent.source == "lease_controller"))
        )
        assert events and all(w["token"] not in str(e.payload) for e in events)


def test_expiry_is_durable_and_never_renewed(workspace):
    w = workspace
    m = DurableLeaseManager(w["factory"])
    with w["factory"].begin() as s:
        s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
    a = m.expire(HUMAN, w["run"], 1, "expired")
    assert a.revoked and a.generation == 2
    with pytest.raises(DomainError):
        m.heartbeat(w["actor"], w["run"], heartbeat(w), "no-revive")


def test_human_cancel_revokes_in_same_transaction(workspace):
    w = workspace
    with w["factory"]() as s:
        version = s.get(PrototypeRun, w["run"]).lease_generation
        from hogwarts.models import Proposal

        pv = s.get(Proposal, w["proposal"]).version
    command(
        w["factory"],
        HUMAN,
        "cancel",
        "proposal.cancel",
        w["proposal"],
        {},
        lambda s: cancel(s, HUMAN, w["proposal"], VersionCommand(expected_version=pv), "cancel"),
    )
    with w["factory"]() as s:
        run = s.get(PrototypeRun, w["run"])
        assert run.lease_generation == version + 1 and run.lease_revoked_at is not None
        assert run.lease_stop_state == "PENDING"


def test_commit_time_expiry_rejects_late_write(workspace, monkeypatch):
    w = workspace
    from hogwarts.leases import fencing

    original = now()
    with pytest.raises(DomainError):
        with w["factory"].begin() as s:
            w["manager"]._authorize(s, w["actor"], w["run"], w["token"])
            run = s.get(PrototypeRun, w["run"])
            run.evidence = {"late": True}
            monkeypatch.setattr(fencing, "now", lambda: original + timedelta(hours=1))
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).evidence is None


def test_late_review_revocation_rejected(verification):
    v = verification
    info = verify(v)
    DurableLeaseManager(v[0]["factory"]).revoke(HUMAN, v[0]["run"], 1, "review-revoke")
    with pytest.raises(DomainError):
        review(v, info)


def test_revoke_inside_qa_rejects_late_results(verification, monkeypatch):
    v = verification
    real = v[3].execute

    def execute(*args, **kwargs):
        result = real(*args, **kwargs)
        DurableLeaseManager(v[0]["factory"]).revoke(HUMAN, v[0]["run"], 1, "late-qa")
        return result

    monkeypatch.setattr(v[3], "execute", execute)
    with pytest.raises(DomainError):
        verify(v)
    with pytest.raises(DomainError):
        verify(v)
    assert v[3].calls.count("execute") == 1


@pytest.mark.parametrize("bad", ["wrong_owner", "worker", "stale_generation", "not_expired", "key"])
def test_revocation_authorization_and_expiry_guard(workspace, bad):
    w = workspace
    m = DurableLeaseManager(w["factory"])
    actor = replace(HUMAN, owner_id="other") if bad == "wrong_owner" else HUMAN
    if bad == "worker":
        actor = w["actor"]
    with pytest.raises(DomainError):
        if bad == "not_expired":
            m.expire(actor, w["run"], 1, "early")
        else:
            m.revoke(
                actor,
                w["run"],
                0 if bad == "stale_generation" else 1,
                "../unsafe" if bad == "key" else "denied",
            )


def test_database_failure_rolls_back_heartbeat(workspace):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    w = workspace

    def fail(s):
        if s.info.get("lease_fences"):
            raise OperationalError("injected", {}, Exception("do not expose"))

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(DomainError) as error:
            DurableLeaseManager(w["factory"]).heartbeat(
                w["actor"], w["run"], heartbeat(w), "db-fail"
            )
        assert error.value.code == "lease_database_unavailable"
    finally:
        event.remove(Session, "before_commit", fail)
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).lease_version == 1


def test_revoke_serializes_with_publication_transaction(workspace):
    from threading import Event

    w = workspace
    started = Event()

    def revoke():
        started.set()
        return DurableLeaseManager(w["factory"]).revoke(HUMAN, w["run"], 1, "race")

    with ThreadPoolExecutor(1) as pool:
        with w["factory"].begin() as s:
            run, _, _ = w["manager"]._authorize(s, w["actor"], w["run"], w["token"])
            future = pool.submit(revoke)
            assert started.wait(5)
            assert not future.done()
            run.evidence = {"ordered_before_revoke": True}
        assert future.result(timeout=30).revoked
    with pytest.raises(DomainError):
        with w["factory"].begin() as s:
            w["manager"]._authorize(s, w["actor"], w["run"], w["token"])
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).evidence == {"ordered_before_revoke": True}


def test_late_artifact_publication_rolls_back(artifacts, monkeypatch):
    from hogwarts.leases import fencing
    from hogwarts.models import ArtifactMetadata
    from test_artifacts import put

    a = artifacts
    publish = a[3].objects.publish
    stamp = now()

    def late(*args):
        result = publish(*args)
        monkeypatch.setattr(fencing, "now", lambda: stamp + timedelta(hours=1))
        return result

    monkeypatch.setattr(a[3].objects, "publish", late)
    with pytest.raises(DomainError):
        put(a)
    with a[0]["factory"]() as s:
        assert all(r.state != "READY" for r in s.scalars(select(ArtifactMetadata)))


def test_completed_review_survives_ordinary_expiry(verification):
    v = verification
    info = verify(v)
    with v[0]["factory"].begin() as s:
        s.get(PrototypeRun, v[0]["run"]).lease_expires_at = now() - timedelta(seconds=1)
    assert review(v, info)["merged"] is False


def test_migration_backfill_and_roundtrip(workspace):
    from alembic import command as migration
    from alembic.config import Config
    from sqlalchemy import inspect

    w = workspace
    engine = w["factory"].kw["bind"]
    c = Config("alembic.ini")
    assert "lease_generation" in {x["name"] for x in inspect(engine).get_columns("prototype_runs")}
    migration.check(c)
    migration.downgrade(c, "0006")
    assert "lease_generation" not in {
        x["name"] for x in inspect(engine).get_columns("prototype_runs")
    }
    migration.upgrade(c, "head")
    with w["factory"]() as s:
        run = s.get(PrototypeRun, w["run"])
        assert run.lease_generation == run.lease_version == 1
        assert run.lease_token == w["token"] and run.lease_revoked_at is None
    migration.check(c)


@pytest.mark.parametrize("bad", ["ttl", "extra", "bool_version", "negative"])
def test_strict_lease_contracts(workspace, bad):
    from pydantic import ValidationError

    w = workspace
    with pytest.raises((ValueError, ValidationError)):
        if bad == "ttl":
            DurableLeaseManager(w["factory"], ttl_seconds=301)
        else:
            changes = (
                {"host_path": "/etc/passwd"}
                if bad == "extra"
                else {"expected_version": True}
                if bad == "bool_version"
                else {"generation": -1}
            )
            heartbeat(w, **changes)


def test_stop_commit_failure_retains_pending_for_restart(workspace):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    w = workspace
    m = DurableLeaseManager(w["factory"])
    m.revoke(HUMAN, w["run"], 1, "revoke-stop")

    def fail(s):
        if any(isinstance(x, WorkflowEvent) and x.event_type == "lease.stopped" for x in s.new):
            raise OperationalError("injected", {}, Exception("private"))

    event.listen(Session, "before_commit", fail)
    calls = []
    try:
        with pytest.raises(DomainError):
            m.stop(HUMAN, w["run"], lambda: calls.append("idempotent-stop"))
    finally:
        event.remove(Session, "before_commit", fail)
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).lease_stop_state == "PENDING"
    assert (
        DurableLeaseManager(w["factory"])
        .stop(HUMAN, w["run"], lambda: calls.append("idempotent-stop"))
        .stop_state
        == "CONFIRMED"
    )
    assert len(calls) == 2


def test_changed_generation_rejects_original_worker(workspace):
    w = workspace
    with w["factory"].begin() as s:
        s.get(PrototypeRun, w["run"]).lease_generation += 1
    with pytest.raises(DomainError):
        w["manager"].create(w["actor"], w["run"], w["token"])
    with pytest.raises(DomainError):
        DurableLeaseManager(w["factory"]).heartbeat(
            w["actor"], w["run"], heartbeat(w, generation=2), "stale-credential"
        )


def test_non_ascii_credential_is_stable_denial(workspace):
    w = workspace
    with pytest.raises(DomainError) as error:
        DurableLeaseManager(w["factory"]).heartbeat(
            w["actor"], w["run"], heartbeat(w, lease_token="不可信-token"), "unicode-denied"
        )
    assert error.value.code == "inactive_lease"


def test_revoked_queued_run_cannot_be_claimed(workspace):
    from hogwarts.models import Proposal
    from hogwarts.service import claim

    w = workspace
    with w["factory"].begin() as s:
        run = s.get(PrototypeRun, w["run"])
        run.status = "QUEUED"
        run.lease_token = None
        run.lease_generation = run.lease_issued_generation = run.lease_version = 0
    DurableLeaseManager(w["factory"]).revoke(HUMAN, w["run"], 0, "queued-revoke")
    with w["factory"]() as s:
        pv = s.get(Proposal, w["proposal"]).version
    with pytest.raises(DomainError):
        command(
            w["factory"],
            w["actor"],
            "claim-revoked",
            "run.claim",
            w["run"],
            {},
            lambda s: claim(
                s, w["actor"], w["run"], VersionCommand(expected_version=pv), "claim-revoked"
            ),
        )
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).lease_token is None
        assert s.get(Proposal, w["proposal"]).version == pv
