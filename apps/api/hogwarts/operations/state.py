import json
import re
from functools import wraps
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from hogwarts.models import IdempotencyRecord, WorkflowEvent
from hogwarts.reconciliation.contracts import deny
from hogwarts.service import DomainError, digest


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", value):
        deny("operations_invalid_key", 403)
    return value


def guarded(method):
    @wraps(method)
    def call(self, actor, run_id, *args, **kwargs):
        try:
            return method(self, actor, run_id, *args, **kwargs)
        except (DomainError, SQLAlchemyError, OSError, ValueError, TypeError, KeyError) as error:
            code = (
                error.code
                if isinstance(error, DomainError)
                else (
                    "operations_database_unavailable"
                    if isinstance(error, SQLAlchemyError)
                    else "operations_state_unavailable"
                )
            )
            try:
                with self.factory.begin() as s:
                    self.event(s, actor, run_id, "denied", code=code)
            except SQLAlchemyError:
                deny("operations_database_and_audit_unavailable", 503)
            if isinstance(error, DomainError):
                raise
            deny(code, 503)

    return call


class OperationalState:
    def __init__(self, workspaces):
        self.workspaces, self.factory = workspaces, workspaces.factory

    def context(self, s, actor, run_id, token=None, *, admin=False, live=False):
        if admin and actor.role != "human":
            deny("operations_owner_human_required", 403)
        run, proposal, project = self.workspaces._authorize(
            s, actor, run_id, token, cleanup=not live
        )
        self.workspaces._configuration(proposal, project)
        return run, proposal, project

    def event(self, s, actor, run_id, action, **payload):
        s.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=payload.get("version", 0),
                event_type="operations." + action,
                source="operations",
                actor_id=actor.id,
                reason=action,
                payload=payload,
            )
        )

    def find(self, s, actor, namespace, *, lock=False):
        row = s.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == actor.owner_id,
                IdempotencyRecord.actor_id == "operations.v1",
                IdempotencyRecord.key == namespace,
            )
        )
        if row and lock:
            s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id)
                .values(request_hash=IdempotencyRecord.request_hash)
            )
            s.refresh(row)
        return row

    def value(self, row, model, owner):
        result = model.model_validate_json(json.dumps(row.response))
        if result.owner_id != owner or row.request_hash != digest([row.key, owner, result.version]):
            deny("operations_journal_binding_mismatch", 403)
        return result

    def save(self, s, actor, namespace, value, row=None):
        request_hash = digest([namespace, actor.owner_id, value.version])
        if row:
            prior = digest([namespace, actor.owner_id, value.version - 1])
            changed = s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id, IdempotencyRecord.request_hash == prior)
                .values(request_hash=request_hash, response=value.model_dump(mode="json"))
            )
            if changed.rowcount != 1:
                deny("operations_cas_conflict")
        else:
            s.add(
                IdempotencyRecord(
                    id=getattr(value, "plan_id", str(uuid4())),
                    owner_id=actor.owner_id,
                    actor_id="operations.v1",
                    key=namespace,
                    request_hash=request_hash,
                    response=value.model_dump(mode="json"),
                )
            )
        return value
