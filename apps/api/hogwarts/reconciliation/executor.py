"""No pruning, no replay. An uncertain committed intent is cleanup-only UNKNOWN."""

import json

from sqlalchemy import or_, select, update

from hogwarts.models import GitWorkspace, IdempotencyRecord, PrototypeRun, SandboxInstance, now
from hogwarts.qa.contracts import QAJournal
from hogwarts.reconciliation.contracts import deny
from hogwarts.reconciliation.identity import IdentityVerifier, StateComparator
from hogwarts.service import DomainError


class CleanupExecutor:
    def __init__(self, planner):
        self.p = planner

    def execute(self, actor, run_id, plan_id, expected_version):
        p = self.p
        with p.factory.begin() as s:
            run, proposal, project = p.context(s, actor, run_id)
            row, j = p.row(s, actor, run_id, plan_id)
            if j.state == "COMPLETE" and expected_version in {1, j.version}:
                return j
            if j.version != expected_version:
                deny("cleanup_cas_conflict")
            if j.state == "EXECUTING":
                return p.transition(
                    s, actor, row, j, "UNKNOWN", error_code="cleanup_execution_uncertain"
                )
            if j.state in {"UNKNOWN", "QUARANTINED"}:
                return j
            if project.id != j.project_id:
                deny("cleanup_project_mismatch", 403)
            # No deletion can occur before this durable execution intent commits.
            j = p.transition(s, actor, row, j, "EXECUTING")
        completed = []
        try:
            with p.factory.begin() as s:
                run, proposal, project = p.context(s, actor, run_id)
                row, current = p.row(s, actor, run_id, plan_id)
                if current.state != "EXECUTING" or current.version != j.version:
                    deny("cleanup_execution_uncertain")
                if p.execution_guard:
                    p.execution_guard(s, actor, run_id)
                binding, resources = p.inventory.inspect(s, run, proposal, project)
                conflict = (
                    "cleanup_lease_changed"
                    if (
                        run.lease_generation != j.generation or run.lease_version != j.lease_version
                    )
                    else "cleanup_binding_changed"
                    if binding != j.binding_sha256
                    else "cleanup_resources_changed"
                    if resources != j.resources
                    else "cleanup_inventory_quarantined"
                    if any(x.disposition == "QUARANTINE" for x in resources)
                    else None
                )
                if conflict:
                    return p.transition(
                        s,
                        actor,
                        row,
                        current,
                        "QUARANTINED",
                        error_code=conflict,
                    )
                order = {"container": 0, "volume": 1, "worktree": 2}
                targets = sorted(
                    (x for x in resources if IdentityVerifier.removable(x)),
                    key=lambda x: (order[x.kind], x.key.endswith(":holder"), x.key),
                )
                if targets:
                    changed = s.execute(
                        update(PrototypeRun)
                        .where(
                            PrototypeRun.id == run_id,
                            PrototypeRun.lease_generation == j.generation,
                            PrototypeRun.lease_version == j.lease_version,
                            or_(
                                PrototypeRun.lease_revoked_at.is_not(None),
                                PrototypeRun.lease_expires_at <= now(),
                            ),
                        )
                        .values(lease_version=PrototypeRun.lease_version)
                        .execution_options(synchronize_session=False)
                    )
                    if changed.rowcount != 1:
                        deny("cleanup_generation_fence_failed")
                for target in targets:
                    if p.execution_guard:
                        p.execution_guard(s, actor, run_id)
                    # Re-inspect after earlier effects, immediately before each delete.
                    _, observed = p.inventory.inspect(s, run, proposal, project)
                    match = next((x for x in observed if x.key == target.key), None)
                    if match != target or any(x.disposition == "QUARANTINE" for x in observed):
                        deny("cleanup_target_changed")
                    if target.kind == "worktree":
                        ws = s.get(GitWorkspace, target.identifier)
                        # Private root + exact DB row + Git registration were rechecked.
                        repo = p.workspaces.grants[ws.project_id].repository
                        p.workspaces.git.remove(repo, p.workspaces._path(ws))
                    else:
                        p.inventory.backend.remove(target)
                    completed.append(target.key)
                # Absence is confirmed by a fresh daemon/Git inventory, never inferred
                # from an exit code. Evidence and unknown state are not deleted.
                _, remaining = p.inventory.inspect(s, run, proposal, project)
                if any(x.key in completed for x in remaining):
                    deny("cleanup_absence_unconfirmed")
                if not StateComparator.active(run):
                    ws = s.scalar(select(GitWorkspace).where(GitWorkspace.run_id == run_id))
                    if ws and ws.state != "CLEANED":
                        p.workspaces._state(s, ws, actor, "CLEANED")
                    sandbox = s.scalar(
                        select(SandboxInstance).where(SandboxInstance.run_id == run_id)
                    )
                    if sandbox and p.inventory.backend and sandbox.state != "DESTROYED":
                        sandbox.state = "DESTROYED"
                        sandbox.version += 1
                    for qr in s.scalars(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.owner_id == actor.owner_id,
                            IdempotencyRecord.key.like("qa-runner.v1:%"),
                        )
                    ):
                        if qr.response.get("run_id") == run_id:
                            qj = QAJournal.model_validate_json(json.dumps(qr.response))
                            qr.response = qj.model_copy(
                                update={
                                    "cleanup_requested": True,
                                    "cleanup_complete": True,
                                    "version": qj.version + 1,
                                }
                            ).model_dump(mode="json")
                return p.transition(s, actor, row, current, "COMPLETE", completed=completed)
        except (DomainError, OSError, ValueError, TypeError) as error:
            # Effects may have happened. Never auto-replay this execution intent.
            with p.factory.begin() as s:
                p.context(s, actor, run_id)
                row, current = p.row(s, actor, run_id, plan_id)
                if current.state == "COMPLETE":
                    return current
                return p.transition(
                    s,
                    actor,
                    row,
                    current,
                    "UNKNOWN",
                    completed=completed,
                    error_code=error.code
                    if isinstance(error, DomainError)
                    else "cleanup_io_failed",
                )
