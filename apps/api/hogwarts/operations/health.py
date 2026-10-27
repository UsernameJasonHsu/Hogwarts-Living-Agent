"""Owner-only local snapshots; no secret/path or global production-ready claim."""

from sqlalchemy import func, select

from hogwarts.models import BudgetReservation, WorkflowEvent
from hogwarts.operations.references import ReferenceProtector
from hogwarts.operations.state import OperationalState, guarded
from hogwarts.service import DomainError


class OperationalHealth(OperationalState):
    def __init__(self, quota, *, manual_incidents=(), store=None):
        super().__init__(quota.workspaces)
        self.quota, self.manual_incidents = quota, tuple(manual_incidents)
        self.references = ReferenceProtector(self.workspaces, store)

    @guarded
    def _snapshot(self, actor, run_id):
        with self.factory.begin() as s:
            run, proposal, project = self.context(s, actor, run_id, admin=True)
            _, ledger = self.quota.ledger(s, actor)
            usage = self.quota.storage.measure(s, actor.owner_id)
            _, resources = self.quota.inventory.inspect(s, run, proposal, project)
            _, held = self.references.inspect(s, actor, run, proposal)
            budget = list(
                s.scalars(
                    select(BudgetReservation).where(BudgetReservation.owner_id == actor.owner_id)
                )
            )
            events = dict(
                s.execute(
                    select(WorkflowEvent.event_type, func.count())
                    .where(
                        WorkflowEvent.owner_id == actor.owner_id,
                        WorkflowEvent.source == "operations",
                    )
                    .group_by(WorkflowEvent.event_type)
                    .limit(100)
                ).all()
            )
            codes = set(held)
            if self.manual_incidents:
                codes.add("operations_manual_quarantine_pending")
            if any(r.disposition == "QUARANTINE" for r in resources):
                codes.add("operations_quarantined_inventory")
            if any(r.state in {"IN_USE", "UNKNOWN"} for r in ledger.reservations):
                codes.add("operations_execution_unsettled")
            if usage.free_bytes_min < self.quota.storage.min_free_bytes:
                codes.add("quota_disk_headroom_exhausted")
            if usage.incomplete_artifact_count:
                codes.add("storage_unpublished_artifacts")
            total = self.quota.total(ledger).model_dump()
            limits = self.quota.limits[actor.owner_id].model_dump()
            if any(
                total[k]
                + (
                    usage.logical_bytes + usage.unmaterialized_artifact_bytes
                    if k == "storage_bytes"
                    else 0
                )
                > limits[k]
                for k in total
            ):
                codes.add("quota_capacity_exceeded")
            return {
                "schema_version": 1,
                "status": "blocked" if codes else "healthy",
                "scope": "configured_owner_roots_and_current_run",
                "codes": sorted(codes),
                "quota_version": ledger.version,
                "event_counts": events,
                "held_capacity": total,
                "limits": limits,
                "storage": usage.model_dump(),
                "quarantined_current_run_resources": sum(
                    r.disposition == "QUARANTINE" for r in resources
                ),
                "manual_incident_count": len(self.manual_incidents),
                "budget_reserved_estimate_cents": sum(
                    b.estimate_cents for b in budget if b.status == "RESERVED"
                ),
                "budget_recorded_actual_cents_known": sum(
                    b.actual_cents for b in budget if b.actual_cents is not None
                ),
                "budget_unknown_actual_rows": sum(b.actual_cents is None for b in budget),
                "m3_ready": False,
            }

    def snapshot(self, actor, run_id):
        try:
            return self._snapshot(actor, run_id)
        except DomainError as error:
            if error.status in {403, 404}:
                raise
            return {
                "schema_version": 1,
                "status": "unknown",
                "codes": [error.code],
                "m3_ready": False,
            }
