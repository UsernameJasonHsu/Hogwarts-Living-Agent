"""Reviewed ephemeral GC; irreversible operations remain in the exact Reconciler."""

from datetime import datetime, timedelta
from uuid import uuid4

from hogwarts.models import IdempotencyRecord, now
from hogwarts.operations.contracts import GCJournal, VersionedRetentionPolicy
from hogwarts.operations.references import ReferenceProtector
from hogwarts.operations.state import OperationalState, guarded, key
from hogwarts.reconciliation.contracts import deny
from hogwarts.reconciliation.executor import CleanupExecutor
from hogwarts.reconciliation.identity import IdentityVerifier, StateComparator
from hogwarts.reconciliation.planner import CleanupPlanner
from hogwarts.service import DomainError, digest, utc


class SafeGarbageCollectionPlanner(OperationalState):
    def __init__(self, workspaces, policy, *, backend=None, store=None):
        super().__init__(workspaces)
        if type(policy) is not VersionedRetentionPolicy:
            deny("retention_invalid_policy", 403)
        self.policy = policy
        self.backend = backend
        self.cleanup = CleanupPlanner(workspaces, backend)
        self.references = ReferenceProtector(workspaces, store)

    def observation(self, s, actor, run_id):
        run, proposal, project = self.context(s, actor, run_id, admin=True)
        reference_hash, held = self.references.inspect(s, actor, run, proposal)
        binding, resources = self.cleanup.inventory.inspect(s, run, proposal, project)
        reasons = set(held)
        if StateComparator.active(run):
            reasons.add("retention_active_lease")
        origin = run.ended_at or run.lease_revoked_at or run.lease_expires_at
        if not origin or utc(origin) + timedelta(seconds=self.policy.ephemeral_ttl_seconds) > now():
            reasons.add("retention_not_expired")
        if any(r.disposition == "QUARANTINE" for r in resources):
            reasons.add("retention_quarantined_inventory")
        return (
            run,
            reference_hash,
            digest([binding, [r.model_dump() for r in resources]]),
            resources,
            tuple(sorted(reasons)),
        )

    def row(self, s, actor, run_id, identifier):
        row = s.get(IdempotencyRecord, identifier)
        if (
            not row
            or row.owner_id != actor.owner_id
            or row.actor_id != "operations.v1"
            or not row.key.startswith("retention-gc.v1:")
        ):
            deny("retention_plan_not_found", 404)
        j = self.value(row, GCJournal, actor.owner_id)
        if j.plan_id != identifier or j.run_id != run_id or j.actor_id != actor.id:
            deny("retention_plan_binding_mismatch", 403)
        return row, j

    def change(self, s, actor, row, j, **fields):
        new = j.model_copy(update={**fields, "version": j.version + 1})
        self.save(s, actor, row.key, new, row)
        self.event(
            s,
            actor,
            j.run_id,
            "gc_" + new.state.lower(),
            version=new.version,
            plan_id=new.plan_id,
            code=new.error_code,
        )
        return new

    def check(self, s, actor, run_id, j, *, deleting=False):
        run, refs, resources_hash, resources, reasons = self.observation(s, actor, run_id)
        if (run.lease_generation, run.lease_version) != (j.generation, j.lease_version):
            deny("retention_generation_changed")
        if digest(self.policy.model_dump()) != j.policy_sha256:
            deny("retention_policy_changed")
        if refs != j.references_sha256 or reasons:
            deny("retention_references_or_hold_changed")
        if not deleting and resources_hash != j.resources_sha256:
            deny("retention_resources_changed")
        return resources

    @guarded
    def plan(self, actor, run_id, request_key):
        key(request_key)
        namespace = "retention-gc.v1:" + request_key
        # Planning itself only creates immutable observations/audit; no deletes.
        cleanup = self.cleanup.plan(actor, run_id, "gc_" + request_key)
        with self.factory.begin() as s:
            self.context(s, actor, run_id, admin=True)
            prior = self.find(s, actor, namespace)
            if prior:
                return self.row(s, actor, run_id, prior.id)[1]
            run, refs, resource_hash, resources, reasons = self.observation(s, actor, run_id)
            if not any(IdentityVerifier.removable(x) for x in resources):
                reasons = tuple(sorted(set(reasons) | {"retention_no_eligible_resources"}))
            # Never combine a cleanup observation from a different moment.
            _, p, project = self.context(s, actor, run_id, admin=True)
            binding, observed = self.cleanup.inventory.inspect(s, run, p, project)
            if cleanup.binding_sha256 != binding or cleanup.resources != observed:
                deny("retention_plan_observation_changed")
            j = GCJournal(
                plan_id=str(uuid4()),
                run_id=run_id,
                owner_id=actor.owner_id,
                actor_id=actor.id,
                generation=run.lease_generation,
                lease_version=run.lease_version,
                cleanup_id=cleanup.plan_id,
                cleanup_version=cleanup.version,
                policy_sha256=digest(self.policy.model_dump()),
                references_sha256=refs,
                resources_sha256=resource_hash,
                created_at=now().isoformat(),
                eligible=not reasons,
                reasons=reasons,
            )
            self.save(s, actor, namespace, j)
            self.event(
                s,
                actor,
                run_id,
                "gc_dry_run",
                version=1,
                plan_id=j.plan_id,
                eligible=j.eligible,
                reasons=list(reasons),
            )
            return j

    @guarded
    def approve(self, actor, run_id, identifier, expected_version):
        with self.factory.begin() as s:
            self.context(s, actor, run_id, admin=True)
            row, j = self.row(s, actor, run_id, identifier)
            if type(expected_version) is not int:
                deny("retention_cas_conflict")
            if j.state == "APPROVED" and expected_version == j.version - 1:
                return j
            if type(expected_version) is not int or j.version != expected_version:
                deny("retention_cas_conflict")
            if j.state != "DRY_RUN" or not j.eligible:
                deny("retention_not_eligible")
            self.check(s, actor, run_id, j)
            return self.change(
                s,
                actor,
                row,
                j,
                state="APPROVED",
                approved_by=actor.id,
                approval_expires_at=(
                    now() + timedelta(seconds=self.policy.approval_ttl_seconds)
                ).isoformat(),
            )

    def gate(self, s, actor, run_id, identifier):
        # Called within the Reconciler's Run lock before each exact deletion.
        _, j = self.row(s, actor, run_id, identifier)
        if (
            j.state != "EXECUTING"
            or j.approved_by != actor.id
            or not j.approval_expires_at
            or utc(datetime.fromisoformat(j.approval_expires_at)) <= now()
        ):
            deny("retention_approval_invalid")
        self.check(s, actor, run_id, j, deleting=True)

    @guarded
    def execute(self, actor, run_id, identifier, expected_version):
        with self.factory.begin() as s:
            self.context(s, actor, run_id, admin=True)
            row, j = self.row(s, actor, run_id, identifier)
            if j.state == "COMPLETE" and expected_version in {2, j.version}:
                return j
            if type(expected_version) is not int or j.version != expected_version:
                deny("retention_cas_conflict")
            if j.state in {"UNKNOWN", "BLOCKED"}:
                return j
            if j.state == "EXECUTING":
                return self.change(
                    s, actor, row, j, state="UNKNOWN", error_code="retention_execution_uncertain"
                )
            if j.state != "APPROVED" or j.approved_by != actor.id or not j.approval_expires_at:
                deny("retention_human_approval_required", 403)
            try:
                if utc(datetime.fromisoformat(j.approval_expires_at)) <= now():
                    deny("retention_approval_expired")
                self.check(s, actor, run_id, j)
            except DomainError as error:
                return self.change(s, actor, row, j, state="BLOCKED", error_code=error.code)
            j = self.change(s, actor, row, j, state="EXECUTING")
        try:
            isolated = CleanupPlanner(self.workspaces, self.backend)
            isolated.execution_guard = lambda s, a, r: self.gate(s, a, r, identifier)
            result = CleanupExecutor(isolated).execute(
                actor, run_id, j.cleanup_id, j.cleanup_version
            )
            state = "COMPLETE" if result.state == "COMPLETE" else "UNKNOWN"
            code = result.error_code
        except (DomainError, OSError, ValueError) as error:
            state, code = (
                "UNKNOWN",
                error.code if isinstance(error, DomainError) else "retention_io_uncertain",
            )
        with self.factory.begin() as s:
            self.context(s, actor, run_id, admin=True)
            row, current = self.row(s, actor, run_id, identifier)
            if current.version != j.version or current.state != "EXECUTING":
                deny("retention_execution_uncertain")
            return self.change(s, actor, row, current, state=state, error_code=code)
