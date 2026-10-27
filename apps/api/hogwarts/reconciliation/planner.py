"""Explicit owner-controlled plans; durable intent and Run-first CAS fencing."""

import json
import re
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from hogwarts.models import IdempotencyRecord, WorkflowEvent, now
from hogwarts.reconciliation.contracts import CleanupJournal, deny
from hogwarts.reconciliation.inventory import ResourceInventory
from hogwarts.service import DomainError, digest


def journal(row):
    return CleanupJournal.model_validate_json(json.dumps(row.response))


class CleanupPlanner:
    def __init__(self, workspaces, backend=None):
        self.workspaces, self.factory = workspaces, workspaces.factory
        self.inventory = ResourceInventory(workspaces, backend)
        # Trusted operator wiring only; never supplied through an Agent request.
        self.execution_guard = None

    def denied(self, actor, run_id, code):
        try:
            with self.factory.begin() as s:
                s.add(
                    WorkflowEvent(
                        owner_id=actor.owner_id,
                        aggregate_id=run_id,
                        aggregate_version=0,
                        event_type="cleanup.denied",
                        source="resource_reconciler",
                        actor_id=actor.id,
                        reason="denied",
                        payload={"code": code},
                    )
                )
        except SQLAlchemyError:
            deny("cleanup_database_and_audit_unavailable", 503)

    def context(self, s, actor, run_id):
        if actor.role != "human":
            deny("cleanup_owner_human_required", 403)
        return self.workspaces._authorize(s, actor, run_id, None, cleanup=True)

    def event(self, s, actor, j, event, code=None):
        s.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=j.run_id,
                aggregate_version=j.version,
                event_type="cleanup." + event,
                source="resource_reconciler",
                actor_id=actor.id,
                reason=event,
                payload={
                    "plan_id": j.plan_id,
                    "state": j.state,
                    "generation": j.generation,
                    "code": code,
                    "completed": list(j.completed),
                },
            )
        )

    def row(self, s, actor, run_id, plan_id):
        row = s.get(IdempotencyRecord, plan_id)
        if (
            not row
            or row.owner_id != actor.owner_id
            or row.actor_id != actor.id
            or not row.key.startswith("resource-cleanup.v1:")
        ):
            deny("cleanup_plan_not_found", 404)
        s.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == row.id)
            .values(request_hash=IdempotencyRecord.request_hash)
        )
        s.refresh(row)
        j = journal(row)
        if (
            j.plan_id != row.id
            or j.run_id != run_id
            or j.owner_id != actor.owner_id
            or j.actor_id != actor.id
            or row.request_hash != digest([run_id, row.key, j.version])
        ):
            deny("cleanup_journal_binding_mismatch", 403)
        return row, j

    def transition(self, s, actor, row, j, state, **values):
        new = CleanupJournal.model_validate_json(
            json.dumps(
                {**j.model_dump(mode="json"), **values, "state": state, "version": j.version + 1}
            )
        )
        prior = digest([j.run_id, row.key, j.version])
        next_hash = digest([j.run_id, row.key, new.version])
        changed = s.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == row.id, IdempotencyRecord.request_hash == prior)
            .values(response=new.model_dump(mode="json"), request_hash=next_hash)
        )
        if changed.rowcount != 1:
            deny("cleanup_cas_conflict")
        row.request_hash = next_hash
        row.response = new.model_dump(mode="json")
        self.event(s, actor, new, state.lower(), new.error_code)
        return new

    def plan(self, actor, run_id, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", key):
            deny("cleanup_invalid_key", 403)
        key = "resource-cleanup.v1:" + key
        try:
            with self.factory.begin() as s:
                run, proposal, project = self.context(s, actor, run_id)
                prior = s.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.owner_id == actor.owner_id,
                        IdempotencyRecord.actor_id == actor.id,
                        IdempotencyRecord.key == key,
                    )
                )
                if prior:
                    _, result = self.row(s, actor, run_id, prior.id)
                    return result
                binding, resources = self.inventory.inspect(s, run, proposal, project)
                j = CleanupJournal(
                    plan_id=str(uuid4()),
                    run_id=run.id,
                    owner_id=actor.owner_id,
                    actor_id=actor.id,
                    project_id=project.id,
                    generation=run.lease_generation,
                    lease_version=run.lease_version,
                    binding_sha256=binding,
                    resources=resources,
                    created_at=now().isoformat(),
                )
                s.add(
                    IdempotencyRecord(
                        id=j.plan_id,
                        owner_id=actor.owner_id,
                        actor_id=actor.id,
                        key=key,
                        request_hash=digest([run_id, key, 1]),
                        response=j.model_dump(mode="json"),
                    )
                )
                self.event(s, actor, j, "planned")
                return j
        except SQLAlchemyError:
            deny("cleanup_database_unavailable", 503)
        except DomainError as e:
            self.denied(actor, run_id, e.code)
            raise
        except (OSError, ValueError, TypeError):
            self.denied(actor, run_id, "cleanup_inventory_unavailable")
            deny("cleanup_inventory_unavailable", 503)


class ResourceReconciler(CleanupPlanner):
    def execute(self, actor, run_id, plan_id, expected_version):
        from hogwarts.reconciliation.executor import CleanupExecutor

        if type(expected_version) is not int or expected_version < 1:
            deny("cleanup_invalid_version", 403)
        try:
            return CleanupExecutor(self).execute(actor, run_id, plan_id, expected_version)
        except SQLAlchemyError:
            deny("cleanup_database_unavailable", 503)
        except (ValueError, TypeError, OSError):
            deny("cleanup_invalid_or_unavailable_state", 503)
        except DomainError as e:
            self.denied(actor, run_id, e.code)
            raise
