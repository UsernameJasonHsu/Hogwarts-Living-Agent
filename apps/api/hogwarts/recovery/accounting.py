"""Conservative deterministic-fixture accounting; no inferred provider charges."""

from sqlalchemy import func, select, update

from hogwarts.models import Agent, BudgetReservation, WorkspaceBudget
from hogwarts.recovery.contracts import deny
from hogwarts.service import release


def accounting(s, run, proposal, owner, *, lock=False):
    if lock:
        # Same order as M1 reserve/release, always after the Run lock.
        for model, predicate in (
            (WorkspaceBudget, WorkspaceBudget.owner_id == owner),
            (Agent, Agent.id == proposal.author_agent_id),
        ):
            s.execute(update(model).where(predicate).values(active_runs=model.active_runs))
    reservation = s.scalar(select(BudgetReservation).where(BudgetReservation.run_id == run.id))
    workspace = s.get(WorkspaceBudget, owner)
    agent = s.get(Agent, proposal.author_agent_id)

    def totals(*conditions):
        return tuple(
            s.execute(
                select(
                    func.coalesce(func.sum(BudgetReservation.estimate_cents), 0), func.count()
                ).where(BudgetReservation.status == "RESERVED", *conditions)
            ).one()
        )

    owner_total = totals(BudgetReservation.owner_id == owner)
    agent_total = totals(BudgetReservation.agent_id == proposal.author_agent_id)
    valid = bool(
        reservation
        and workspace
        and agent
        and agent.owner_id == owner
        and reservation.owner_id == owner
        and reservation.agent_id == agent.id
        and reservation.estimate_cents > 0
        and (workspace.reserved_cents, workspace.active_runs) == owner_total
        and (agent.reserved_cents, agent.active_runs) == agent_total
    )
    return {
        "valid": valid,
        "status": reservation.status if reservation else None,
        "actual": reservation.actual_cents if reservation else None,
        "reservation": reservation.id if reservation else None,
        "amount": reservation.estimate_cents if reservation else None,
        "owner_total": owner_total,
        "agent_total": agent_total,
        "workspace": [workspace.reserved_cents, workspace.active_runs] if workspace else None,
        "agent": [agent.reserved_cents, agent.active_runs] if agent else None,
    }


def settle(s, run, proposal, actor, proof):
    data = accounting(s, run, proposal, actor.owner_id, lock=True)
    if not proof or not data["valid"] or data["status"] != "RESERVED" or data["actual"] is not None:
        deny("recovery_accounting_evidence_insufficient")
    release(s, run)
