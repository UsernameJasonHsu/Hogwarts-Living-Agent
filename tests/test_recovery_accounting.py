import pytest
from hogwarts.models import Agent, BudgetReservation, Proposal, PrototypeRun, WorkspaceBudget
from hogwarts.schemas import VersionCommand
from hogwarts.service import DomainError, cancel, command
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from test_recovery import coordinator
from test_recovery_states import restore_reservation, save_accounting_example
from test_verification import HUMAN, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def test_budget_settlement_preserves_other_run_reservation(verification):
    v = verification
    verify(v)
    restore_reservation(v)
    w = v[0]
    with w["factory"].begin() as s:
        original = s.get(Proposal, w["proposal"])
        agent = Agent(owner_id=HUMAN.owner_id, key="other", display_name="Other")
        s.add(agent)
        s.flush()
        proposal = Proposal(
            owner_id=HUMAN.owner_id,
            project_id=original.project_id,
            author_agent_id=agent.id,
            document=original.document,
            status="QUEUED",
        )
        s.add(proposal)
        s.flush()
        other = PrototypeRun(proposal_id=proposal.id, status="QUEUED")
        s.add(other)
        s.flush()
        s.add(
            BudgetReservation(
                run_id=other.id, owner_id=HUMAN.owner_id, agent_id=agent.id, estimate_cents=7
            )
        )
        agent.reserved_cents, agent.active_runs = 7, 1
        budget = s.get(WorkspaceBudget, HUMAN.owner_id)
        budget.reserved_cents += 7
        budget.active_runs += 1
        other_id = other.id
    p = coordinator(v).recover(HUMAN, w["run"], "shared-budget", dry_run=False)
    save_accounting_example(v, p, "other-run-preserved")
    assert p.state == "COMPLETE", p
    with w["factory"]() as s:
        budget = s.get(WorkspaceBudget, HUMAN.owner_id)
        assert (budget.reserved_cents, budget.active_runs) == (7, 1)
        assert (
            s.scalar(select(BudgetReservation).where(BudgetReservation.run_id == other_id)).status
            == "RESERVED"
        )


def test_database_unavailable_does_not_claim_recovery(verification, monkeypatch):
    c = coordinator(verification)

    class Down:
        def begin(self):
            raise OperationalError("injected", {}, Exception("unavailable"))

    monkeypatch.setattr(c.planner, "factory", Down())
    with pytest.raises(DomainError) as e:
        c.recover(HUMAN, verification[0]["run"], "database-down")
    assert e.value.status == 503 and not verification[3].calls


def test_legal_cancelled_terminal_never_resumes(verification):
    v = verification
    w = v[0]
    with w["factory"]() as s:
        version = s.get(Proposal, w["proposal"]).version
    command(
        w["factory"],
        HUMAN,
        "human-cancel",
        "cancel",
        w["proposal"],
        {"version": version},
        lambda s: cancel(
            s, HUMAN, w["proposal"], VersionCommand(expected_version=version), "human-cancel"
        ),
    )
    p = coordinator(v).recover(HUMAN, w["run"], "cancel-terminal", dry_run=False)
    assert p.state == "COMPLETE" and p.observation.decision.action == "NONE", p
    assert not v[3].calls and not v[2].calls


@pytest.mark.parametrize("failed", [False, True])
def test_trusted_m1_fake_terminal_is_recognized(verification, api, failed):
    from conftest import post

    v = verification
    w = v[0]
    if failed:

        class CrashedFake:
            def execute(self, *args, **kwargs):
                raise RuntimeError("injected fake failure")

        api.app.state.adapters.runtime = CrashedFake()
    with w["factory"]() as s:
        version = s.get(Proposal, w["proposal"]).version
    response = post(
        api,
        f"runs/{w['run']}/execute",
        {"expected_version": version, "lease_token": w["token"]},
        "worker",
    )
    assert response.status_code == 200, response.text
    result = coordinator(v).recover(HUMAN, w["run"], "m1-fake-terminal", dry_run=False)
    assert result.state == "COMPLETE", result
    assert not v[3].calls  # Never starts the M2 QA adapter to recover a fake run.
