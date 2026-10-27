"""Durable human triage. Acknowledgment is never a workflow permission grant."""

import json
from uuid import uuid4

from sqlalchemy import select, update

from hogwarts.models import IdempotencyRecord, WorkflowEvent
from hogwarts.recovery.contracts import HumanIntervention, deny
from hogwarts.service import digest

QUEUE_ACTOR = "workflow-recovery-queue"


class HumanInterventionQueue:
    def __init__(self, workspaces):
        self.workspaces, self.factory = workspaces, workspaces.factory

    def put(self, s, actor, journal, reason, observation=None):
        o = observation or journal.observation
        key = "recovery-intervention.v1:" + o.run_id
        row = s.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == actor.owner_id,
                IdempotencyRecord.actor_id == QUEUE_ACTOR,
                IdempotencyRecord.key == key,
            )
        )
        old = HumanIntervention.model_validate_json(json.dumps(row.response)) if row else None
        if old and old.observation_sha256 == o.fingerprint and old.reason == reason:
            return old
        item = HumanIntervention(
            intervention_id=old.intervention_id if old else str(uuid4()),
            owner_id=actor.owner_id,
            run_id=o.run_id,
            recovery_id=journal.recovery_id,
            generation=o.generation,
            observation_sha256=o.fingerprint,
            reason=reason,
            version=old.version + 1 if old else 1,
        )
        if row:
            expected = digest([o.run_id, old.version])
            changed = s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id, IdempotencyRecord.request_hash == expected)
                .values(
                    request_hash=digest([o.run_id, item.version]),
                    response=item.model_dump(mode="json"),
                )
            )
            if changed.rowcount != 1:
                deny("recovery_intervention_cas_conflict")
        else:
            s.add(
                IdempotencyRecord(
                    id=item.intervention_id,
                    owner_id=actor.owner_id,
                    actor_id=QUEUE_ACTOR,
                    key=key,
                    request_hash=digest([o.run_id, 1]),
                    response=item.model_dump(mode="json"),
                )
            )
        s.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=o.run_id,
                aggregate_version=item.version,
                source="workflow_recovery",
                event_type="recovery.intervention",
                actor_id=actor.id,
                reason=reason,
                payload={
                    "intervention_id": item.intervention_id,
                    "recovery_id": journal.recovery_id,
                    "generation": o.generation,
                },
            )
        )
        return item

    def list(self, actor):
        if actor.role != "human":
            deny("recovery_human_required", 403)
        with self.factory() as s:
            return [
                HumanIntervention.model_validate_json(json.dumps(r.response))
                for r in s.scalars(
                    select(IdempotencyRecord)
                    .where(
                        IdempotencyRecord.owner_id == actor.owner_id,
                        IdempotencyRecord.actor_id == QUEUE_ACTOR,
                    )
                    .order_by(IdempotencyRecord.id)
                    .limit(100)
                )
            ]

    def acknowledge(self, actor, intervention_id, expected_version):
        if actor.role != "human" or type(expected_version) is not int:
            deny("recovery_human_required", 403)
        with self.factory.begin() as s:
            row = s.get(IdempotencyRecord, intervention_id)
            if not row or row.owner_id != actor.owner_id or row.actor_id != QUEUE_ACTOR:
                deny("recovery_intervention_not_found", 404)
            item = HumanIntervention.model_validate_json(json.dumps(row.response))
            self.workspaces._authorize(s, actor, item.run_id, None, cleanup=True)
            s.refresh(row)
            item = HumanIntervention.model_validate_json(json.dumps(row.response))
            if item.version != expected_version:
                deny("recovery_intervention_cas_conflict")
            if item.status == "ACKNOWLEDGED":
                return item
            new = item.model_copy(update={"status": "ACKNOWLEDGED", "version": item.version + 1})
            changed = s.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.id == row.id,
                    IdempotencyRecord.request_hash == digest([item.run_id, item.version]),
                )
                .values(
                    request_hash=digest([item.run_id, new.version]),
                    response=new.model_dump(mode="json"),
                )
            )
            if changed.rowcount != 1:
                deny("recovery_intervention_cas_conflict")
            s.add(
                WorkflowEvent(
                    owner_id=actor.owner_id,
                    aggregate_id=item.run_id,
                    aggregate_version=new.version,
                    source="workflow_recovery",
                    event_type="recovery.triaged",
                    actor_id=actor.id,
                    reason="human acknowledgment only",
                    payload={"intervention_id": item.intervention_id},
                )
            )
            return new
