import json
import re
from uuid import uuid4

from sqlalchemy import select, update

from hogwarts.models import IdempotencyRecord, WorkflowEvent, now
from hogwarts.recovery.contracts import RecoveryJournal, deny
from hogwarts.service import digest


class RecoveryPlanner:
    def __init__(self, inspector, interventions):
        self.inspector, self.interventions = inspector, interventions
        self.factory = inspector.workspaces.factory

    def event(self, s, actor, j, event, code=None):
        s.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=j.observation.run_id,
                aggregate_version=j.version,
                source="workflow_recovery",
                event_type="recovery." + event,
                actor_id=actor.id,
                reason=code or event,
                payload={
                    "recovery_id": j.recovery_id,
                    "generation": j.observation.generation,
                    "classification": j.observation.decision.classification,
                    "action": j.observation.decision.action,
                    "code": code,
                },
            )
        )

    def row(self, s, actor, run_id, identifier):
        row = s.get(IdempotencyRecord, identifier)
        if (
            not row
            or row.owner_id != actor.owner_id
            or row.actor_id != actor.id
            or not row.key.startswith("workflow-recovery.v1:")
        ):
            deny("recovery_plan_not_found", 404)
        s.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == row.id)
            .values(request_hash=IdempotencyRecord.request_hash)
        )
        s.refresh(row)
        j = RecoveryJournal.model_validate_json(json.dumps(row.response))
        if (
            j.recovery_id != row.id
            or j.owner_id != actor.owner_id
            or j.actor_id != actor.id
            or j.observation.run_id != run_id
            or row.request_hash != digest([run_id, row.key, j.version])
        ):
            deny("recovery_journal_binding_mismatch")
        return row, j

    def state(self, s, actor, row, j, state, code=None, result=None):
        new = j.model_copy(
            update={"state": state, "version": j.version + 1, "error_code": code, "result": result}
        )
        changed = s.execute(
            update(IdempotencyRecord)
            .where(
                IdempotencyRecord.id == row.id,
                IdempotencyRecord.request_hash
                == digest([j.observation.run_id, row.key, j.version]),
            )
            .values(
                request_hash=digest([j.observation.run_id, row.key, new.version]),
                response=new.model_dump(mode="json"),
            )
        )
        if changed.rowcount != 1:
            deny("recovery_cas_conflict")
        self.event(s, actor, new, state.lower(), code)
        if state in {"MANUAL_REQUIRED", "UNKNOWN"}:
            self.interventions.put(s, actor, new, code or new.observation.decision.reason)
        return new

    def plan(self, actor, run_id, key, token=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", key):
            deny("recovery_invalid_key", 403)
        key = "workflow-recovery.v1:" + key
        with self.factory.begin() as s:
            self.inspector.context(s, actor, run_id, token)
            old = s.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.owner_id == actor.owner_id,
                    IdempotencyRecord.actor_id == actor.id,
                    IdempotencyRecord.key == key,
                )
            )
            if old:
                return self.row(s, actor, run_id, old.id)[1]
            obs, _ = self.inspector.inspect(s, actor, run_id, token)
            j = RecoveryJournal(
                recovery_id=str(uuid4()),
                owner_id=actor.owner_id,
                actor_id=actor.id,
                observation=obs,
                created_at=now().isoformat(),
            )
            s.add(
                IdempotencyRecord(
                    id=j.recovery_id,
                    owner_id=actor.owner_id,
                    actor_id=actor.id,
                    key=key,
                    request_hash=digest([run_id, key, 1]),
                    response=j.model_dump(mode="json"),
                )
            )
            self.event(s, actor, j, "planned")
            if obs.decision.classification == "MANUAL_REQUIRED":
                self.interventions.put(s, actor, j, obs.decision.reason)
            return j
