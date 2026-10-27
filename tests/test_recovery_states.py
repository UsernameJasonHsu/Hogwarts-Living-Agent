"""Publication-only recovery, terminal accounting and transactional failure evidence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from hogwarts.models import (
    Agent,
    ApprovalRequest,
    ArtifactMetadata,
    BudgetReservation,
    IdempotencyRecord,
    PrototypeRun,
    WorkflowEvent,
    WorkspaceBudget,
    now,
)
from hogwarts.schemas import VersionCommand
from hogwarts.service import DomainError, cancel, command
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_recovery import coordinator
from test_verification import HUMAN, review, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def partial(v, monkeypatch, prefix):
    original = v[1].store.put

    def failed(*args, **kwargs):
        if args[5].startswith(prefix):
            if prefix != "vb_":
                original(*args, **kwargs)
            raise OSError("injected partial publication")
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1].store, "put", failed)
    with pytest.raises(DomainError):
        verify(v)
    monkeypatch.setattr(v[1].store, "put", original)


@pytest.mark.parametrize(
    "prefix,action", [("oi_", "IMPORT"), ("pb_", "PATCH"), ("qa_", "QA"), ("vb_", "VERIFICATION")]
)
def test_partial_publication_resumes_without_workload(verification, monkeypatch, prefix, action):
    v = verification
    partial(v, monkeypatch, prefix)
    counts = (v[2].calls.count("execute"), v[2].captures, v[3].calls.count("execute"))
    w = v[0]
    c = coordinator(v)
    p = c.recover(w["actor"], w["run"], "partial", w["token"])
    assert p.observation.decision.action == action, p
    done = c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"])
    assert done.state == "COMPLETE", done
    assert counts == (v[2].calls.count("execute"), v[2].captures, v[3].calls.count("execute"))
    assert c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"]) == done
    with w["factory"]() as s:
        artifacts = list(s.scalars(select(ArtifactMetadata)))
    for a in artifacts:
        assert v[1].store.describe(HUMAN, w["run"], a.id).assessment.status == "unverified"


@pytest.mark.parametrize("action", ["IMPORT", "PATCH", "QA", "VERIFICATION"])
def test_publication_only_entrypoint_never_creates_intent(verification, action):
    v = verification
    w, o = v[:2]
    from uuid import uuid4

    with pytest.raises(DomainError):
        if action == "IMPORT":
            o.importer.resume_publication(
                w["actor"], w["run"], str(uuid4()), str(uuid4()), w["token"]
            )
        elif action == "PATCH":
            o.builder.resume_publication(w["actor"], w["run"], str(uuid4()), w["token"])
        elif action == "QA":
            o.qa.resume_publication(w["actor"], w["run"], str(uuid4()), "acceptance", w["token"])
        else:
            o.resume_publication(w["actor"], w["run"], w["token"], "capture", "acceptance")
    assert not v[2].calls and not v[3].calls


@pytest.mark.parametrize("change", ["generation", "expiry", "cancel", "grant"])
def test_authority_changes_after_plan_block_resume(verification, monkeypatch, change):
    v = verification
    partial(v, monkeypatch, "vb_")
    w = v[0]
    c = coordinator(v)
    p = c.recover(w["actor"], w["run"], "before-change", w["token"])
    if change == "grant":
        w["manager"].grants = {}
    elif change == "cancel":
        from hogwarts.models import Proposal

        with w["factory"]() as s:
            version = s.get(Proposal, w["proposal"]).version
        command(
            w["factory"],
            HUMAN,
            "cancel",
            "cancel",
            w["proposal"],
            {"version": version},
            lambda s: cancel(
                s, HUMAN, w["proposal"], VersionCommand(expected_version=version), "cancel"
            ),
        )
    else:
        with w["factory"].begin() as s:
            run = s.get(PrototypeRun, w["run"])
            if change == "generation":
                run.lease_generation += 1
            else:
                run.lease_expires_at = now() - timedelta(seconds=1)
    try:
        result = c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"])
        assert result.state in {"MANUAL_REQUIRED", "UNKNOWN"}
    except DomainError as error:
        assert error.status in {403, 409}
        assert error.code in {
            "repository_not_granted",
            "inactive_workspace_lease",
            "stale_or_expired_fence",
        }
    assert v[3].calls.count("execute") == 1


@pytest.mark.parametrize("decision", ["ACCEPTED", "REJECTED", "DEFERRED"])
def test_legal_human_terminal_pairs_unchanged(verification, decision):
    from hogwarts.models import Proposal

    v = verification
    result = verify(v)
    review(v, result, decision=decision)
    c = coordinator(v)
    p = c.recover(HUMAN, v[0]["run"], "human-terminal", dry_run=False)
    assert p.state == "COMPLETE", p
    with v[0]["factory"]() as s:
        assert s.get(Proposal, v[0]["proposal"]).status == decision
        assert s.get(PrototypeRun, v[0]["run"]).status == "READY_FOR_HUMAN_REVIEW"


@pytest.mark.parametrize("tamper", ["approval", "artifact", "qa_proof", "profile"])
def test_stale_or_tampered_evidence_requires_human(verification, tamper):
    v = verification
    info = verify(v)
    if tamper == "approval":
        with v[0]["factory"].begin() as s:
            s.get(ApprovalRequest, info.approval_id).expires_at = now() - timedelta(seconds=1)
    elif tamper == "artifact":
        path = v[1].store.objects.path(info.bundle_artifact_id)
        path.chmod(0o600)
        path.write_bytes(b"tampered")
    elif tamper == "qa_proof":
        with v[0]["factory"].begin() as s:
            row = s.scalar(
                select(IdempotencyRecord).where(IdempotencyRecord.key.like("qa-runner.v1:%"))
            )
            row.response = {**row.response, "input_sha256": "f" * 64}
    else:
        v[1].qa.registry.profiles = {}
    p = coordinator(v).recover(HUMAN, v[0]["run"], "tamper", dry_run=False)
    assert p.state == "MANUAL_REQUIRED", p
    assert v[3].calls.count("execute") == 1


def restore_reservation(v):
    w = v[0]
    v[1].cleanup(HUMAN, w["run"])
    with w["factory"].begin() as s:
        row = s.scalar(select(BudgetReservation))
        row.status, row.actual_cents = "RESERVED", None
        budget = s.get(WorkspaceBudget, HUMAN.owner_id)
        agent = s.get(Agent, row.agent_id)
        budget.reserved_cents += row.estimate_cents
        budget.active_runs += 1
        agent.reserved_cents += row.estimate_cents
        agent.active_runs += 1


def save_accounting_example(v, result, name):
    import json
    import os
    from pathlib import Path

    folder = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
    if not folder:
        return
    dest = Path(folder) / ("recovery-accounting-" + name + "-" + v[0]["run"])
    dest.mkdir(parents=True)
    (dest / "recovery-result.json").write_text(result.model_dump_json(indent=2))
    with v[0]["factory"]() as s:
        rows = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "estimate_cents": r.estimate_cents,
                "actual_cents": r.actual_cents,
            }
            for r in s.scalars(select(BudgetReservation))
        ]
        workspace = s.get(WorkspaceBudget, HUMAN.owner_id)
        (dest / "budget-after.json").write_text(
            json.dumps(
                {
                    "reservations": rows,
                    "owner_reserved_cents": workspace.reserved_cents,
                    "owner_active_runs": workspace.active_runs,
                },
                indent=2,
            )
        )


@pytest.mark.parametrize("bad", [None, "counter", "cost", "foreign_reservation", "unknown_qa"])
def test_budget_settlement_requires_accounting_proof(verification, bad):
    v = verification
    verify(v)
    restore_reservation(v)
    with v[0]["factory"].begin() as s:
        row = s.scalar(select(BudgetReservation))
        if bad == "counter":
            s.get(WorkspaceBudget, HUMAN.owner_id).reserved_cents += 1
        elif bad == "cost":
            row.actual_cents = 7
        elif bad == "foreign_reservation":
            row.owner_id = "foreign"
        elif bad == "unknown_qa":
            q = s.scalar(
                select(IdempotencyRecord).where(IdempotencyRecord.key.like("qa-runner.v1:%"))
            )
            q.response = {**q.response, "input_sha256": "e" * 64}
    c = coordinator(v)
    p = c.recover(HUMAN, v[0]["run"], "accounting", dry_run=False)
    assert p.state == ("COMPLETE" if bad is None else "MANUAL_REQUIRED"), p
    with v[0]["factory"]() as s:
        row = s.scalar(select(BudgetReservation))
        assert row.status == ("RELEASED" if bad is None else "RESERVED")
        if bad is None:
            assert row.actual_cents == 0
            assert s.get(WorkspaceBudget, HUMAN.owner_id).reserved_cents == 0
            assert s.get(WorkspaceBudget, HUMAN.owner_id).active_runs == 0
    assert c.execute(HUMAN, v[0]["run"], p.recovery_id, p.version) == p
    save_accounting_example(v, p, bad or "proven-zero")


@pytest.mark.parametrize("phase", ["intent", "final"])
def test_recovery_db_commit_failure_and_restart(verification, monkeypatch, phase):
    v = verification
    partial(v, monkeypatch, "vb_")
    w = v[0]
    c = coordinator(v)
    p = c.recover(w["actor"], w["run"], "db-failure", w["token"])

    def failed(s):
        if any(
            isinstance(x, WorkflowEvent)
            and x.event_type == "recovery." + ("executing" if phase == "intent" else "complete")
            for x in s.new
        ):
            raise OperationalError("injected", {}, Exception("injected"))

    event.listen(Session, "before_commit", failed)
    try:
        if phase == "intent":
            with pytest.raises(DomainError) as e:
                c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"])
            assert e.value.status == 503
        else:
            done = c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"])
            assert done.state == "UNKNOWN", done
    finally:
        event.remove(Session, "before_commit", failed)
    fresh = coordinator(v)
    if phase == "intent":
        assert (
            fresh.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"]).state
            == "COMPLETE"
        )
    else:
        final = fresh.recover(HUMAN, w["run"], "after-unknown", dry_run=False)
        assert final.state == "COMPLETE", final
    assert v[3].calls.count("execute") == 1


def test_concurrent_publication_recovery_executes_once(verification, monkeypatch):
    v = verification
    partial(v, monkeypatch, "vb_")
    w = v[0]
    p = coordinator(v).recover(w["actor"], w["run"], "concurrent", w["token"])
    barrier = Barrier(2)

    def execute():
        barrier.wait()
        try:
            return (
                coordinator(v)
                .execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"])
                .state
            )
        except DomainError:
            return "BUSY"

    with ThreadPoolExecutor(2) as pool:
        states = list(pool.map(lambda _: execute(), range(2)))
    assert "COMPLETE" in states, states
    with w["factory"]() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(WorkflowEvent)
                .where(WorkflowEvent.event_type == "verification.complete")
            )
            == 1
        )
    assert v[3].calls.count("execute") == 1


def test_scan_is_owner_scoped_bounded_and_default_dry_run(verification):
    v = verification
    c = coordinator(v)
    assert len(c.scan(HUMAN, limit=1)) == 1
    with pytest.raises(DomainError):
        c.scan(HUMAN, limit=101)
    from hogwarts.auth import Principal

    assert c.scan(Principal("foreign", "foreign", "human")) == []
    assert not v[3].calls


def test_daemon_unavailable_is_manual_not_absence(verification):
    c = coordinator(verification)

    class Unavailable:
        def inventory(self, *args):
            raise DomainError(503, "docker_unavailable")

    c.inspector.inventory.backend = Unavailable()
    p = c.recover(HUMAN, verification[0]["run"], "daemon-down", dry_run=False)
    assert p.state == "MANUAL_REQUIRED"
    assert p.observation.decision.reason == "recovery_resources_unavailable"
    assert not verification[3].calls


def test_restarted_coordinator_never_replays_executing_intent(verification, monkeypatch):
    from hogwarts.service import digest

    v = verification
    partial(v, monkeypatch, "vb_")
    w = v[0]
    p = coordinator(v).recover(w["actor"], w["run"], "lost-coordinator", w["token"])
    with w["factory"].begin() as s:
        row = s.get(IdempotencyRecord, p.recovery_id)
        row.response = {**row.response, "state": "EXECUTING", "version": 2}
        row.request_hash = digest([w["run"], row.key, 2])
    done = coordinator(v).execute(w["actor"], w["run"], p.recovery_id, 2, w["token"])
    assert done.state == "UNKNOWN" and v[3].calls.count("execute") == 1
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).status == "CLAIMED"


def test_default_scan_ignores_fresh_active_work(verification):
    c = coordinator(verification)
    c.inspector.stale_seconds = 3600
    assert c.scan(HUMAN) == []


def test_recovery_schema_and_credentials_redaction(verification):
    import json
    from pathlib import Path

    import jsonschema

    v = verification
    p = coordinator(v).recover(HUMAN, v[0]["run"], "schema")
    jsonschema.validate(
        p.model_dump(mode="json"),
        json.loads(Path("contracts/recovery-journal.v1.schema.json").read_text(encoding="utf8")),
    )
    assert v[0]["token"] not in p.model_dump_json()
    item = coordinator(v).interventions.list(HUMAN)[0]
    jsonschema.validate(
        item.model_dump(mode="json"),
        json.loads(Path("contracts/human-intervention.v1.schema.json").read_text(encoding="utf8")),
    )
