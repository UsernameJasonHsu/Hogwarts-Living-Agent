"""Preimplementation contracts: real DB/Git/store, explicitly fake workload adapters."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from hogwarts.auth import Principal
from hogwarts.models import BudgetReservation, IdempotencyRecord, PrototypeRun, now
from hogwarts.recovery.contracts import Decision
from hogwarts.recovery.coordinator import CrashRecoveryCoordinator
from hogwarts.service import DomainError
from pydantic import ValidationError
from sqlalchemy import select
from test_verification import HUMAN, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def coordinator(v):
    class FakeResourceAbsence:
        def inventory(self, s, run, workspace):
            return []

    return CrashRecoveryCoordinator(v[1], backend=FakeResourceAbsence(), stale_seconds=0)


def test_recovery_schema_forbids_commands():
    with pytest.raises(ValidationError):
        Decision(
            classification="SAFE_RESUME", action="QA", reason="known_result", shell="echo pass"
        )


def test_default_dry_run_never_executes_or_releases(verification):
    v = verification
    w = v[0]
    c = coordinator(v)
    plan = c.recover(HUMAN, w["run"], "dry")
    assert plan.state == "PLANNED"
    assert plan.observation.decision.classification == "MANUAL_REQUIRED"
    assert not v[2].calls and not v[3].calls
    with w["factory"]() as s:
        assert s.scalar(select(BudgetReservation)).status == "RESERVED"
    assert len(c.interventions.list(HUMAN)) == 1


def test_known_terminal_and_human_decision_are_preserved(verification):
    from test_verification import review

    v = verification
    info = verify(v)
    review(v, info)
    c = coordinator(v)
    p = c.recover(HUMAN, v[0]["run"], "terminal")
    assert p.observation.decision.classification == "FINALIZE"
    done = c.execute(HUMAN, v[0]["run"], p.recovery_id, p.version)
    assert done.state == "COMPLETE"
    assert c.execute(HUMAN, v[0]["run"], p.recovery_id, p.version) == done
    assert v[3].calls.count("execute") == 1


@pytest.mark.parametrize("bad", ["owner", "worker", "lease", "grant"])
def test_authorization_fail_closed(verification, bad):
    v = verification
    w = v[0]
    c = coordinator(v)
    actor, token = w["actor"], w["token"]
    if bad == "owner":
        actor = Principal("other", "foreign", "human")
    elif bad == "worker":
        actor = Principal("worker:other", "local-owner", "worker", "engineer")
    elif bad == "lease":
        token = "wrong"
    else:
        w["manager"].grants = {}
    with pytest.raises(DomainError):
        c.recover(actor, w["run"], "denied", token)
    assert not v[3].calls


def test_expired_unknown_execution_never_replayed(verification):
    v = verification
    with v[0]["factory"].begin() as s:
        s.get(PrototypeRun, v[0]["run"]).lease_expires_at = now() - timedelta(seconds=1)
    c = coordinator(v)
    p = c.recover(HUMAN, v[0]["run"], "expired", dry_run=False)
    assert p.state == "MANUAL_REQUIRED" and not v[3].calls
    with v[0]["factory"]() as s:
        assert s.scalar(select(BudgetReservation)).status == "RESERVED"


def test_queue_acknowledgment_has_no_authority(verification):
    c = coordinator(verification)
    w = verification[0]
    c.recover(HUMAN, w["run"], "triage")
    item = c.interventions.list(HUMAN)[0]
    result = c.interventions.acknowledge(HUMAN, item.intervention_id, item.version)
    assert result.status == "ACKNOWLEDGED"
    with pytest.raises(DomainError):
        c.interventions.acknowledge(HUMAN, item.intervention_id, 999)
    assert not verification[3].calls


def test_concurrent_coordinators_share_plan(verification):
    v = verification
    w = v[0]
    barrier = Barrier(2)

    def request():
        barrier.wait()
        return coordinator(v).recover(HUMAN, w["run"], "same-plan")

    with ThreadPoolExecutor(2) as pool:
        result = list(pool.map(lambda _: request(), range(2)))
    assert result[0] == result[1]
    with w["factory"]() as s:
        assert (
            len(
                list(
                    s.scalars(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.key.like("workflow-recovery.v1:%")
                        )
                    )
                )
            )
            == 1
        )
