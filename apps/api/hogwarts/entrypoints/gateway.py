"""Formal application service. No raw adapters are selected by HTTP/Agent input."""

from uuid import uuid4

from sqlalchemy import select

from hogwarts.entrypoints.boundary import FormalBoundary
from hogwarts.entrypoints.contracts import FormalDispatch
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import (
    ApprovalRequest,
    IdempotencyRecord,
    PrototypeRun,
    SandboxInstance,
    WorkflowEvent,
    now,
)
from hogwarts.operations.contracts import VersionedRetentionPolicy
from hogwarts.operations.controller import OperationalPrototypeController
from hogwarts.operations.health import OperationalHealth
from hogwarts.operations.retention import SafeGarbageCollectionPlanner
from hogwarts.operations.state import OperationalState, guarded
from hogwarts.qa.contracts import QAJournal
from hogwarts.reconciliation.contracts import deny
from hogwarts.recovery.accounting import accounting
from hogwarts.service import DomainError, digest, utc
from hogwarts.workspaces.manager import token_hash


class FormalExecutionGateway(OperationalState):
    def __init__(
        self, quota, orchestrator, profile_name, *, retention_policy=None, manual_incidents=()
    ):
        super().__init__(quota.workspaces)
        if quota.workspaces is not orchestrator.workspaces or profile_name not in quota.profiles:
            deny("formal_graph_binding_mismatch", 403)
        required = set(self.roots(orchestrator))
        if (
            len(quota.limits) != 1
            or len(quota.storage.roots) != 1
            or set(quota.limits) != set(quota.storage.roots)
            or any(set(roots) != required for roots in quota.storage.roots.values())
        ):
            deny("formal_complete_owner_storage_scope_required", 403)
        self.quota, self.orchestrator, self.profile_name = quota, orchestrator, profile_name
        orchestrator.preserve_budget = True
        self.operational = OperationalPrototypeController(quota, orchestrator)
        self.retention = SafeGarbageCollectionPlanner(
            self.workspaces,
            retention_policy or VersionedRetentionPolicy(name="formal-ephemeral"),
            backend=quota.inventory.backend,
            store=orchestrator.store,
        )
        self.health = OperationalHealth(
            quota, manual_incidents=manual_incidents, store=orchestrator.store
        )
        self.boundary = FormalBoundary(self)
        self.operational.execution_scope = self.boundary.execution_scope
        gate = self.retention.gate

        def approved(s, actor, run_id, identifier):
            p = self.boundary.require({"cleanup"})
            if actor != p.actor or run_id != p.run_id:
                deny("formal_cleanup_binding_mismatch", 403)
            gate(s, actor, run_id, identifier)
            _, j = self.retention.row(s, actor, run_id, identifier)
            _, cleanup = self.retention.cleanup.row(s, actor, run_id, j.cleanup_id)
            p.gc_targets = tuple(
                x.identifier
                for x in cleanup.resources
                if x.kind in {"container", "volume"} and x.disposition == "CLEANUP_ELIGIBLE"
            )
            p.gc_workspaces = tuple(
                x.identifier
                for x in cleanup.resources
                if x.kind == "worktree" and x.disposition == "CLEANUP_ELIGIBLE"
            )
            p.gc_validated = True

        self.retention.gate = approved

    @staticmethod
    def roots(o):
        return (
            o.workspaces.root,
            o.store.objects.root,
            o.importer.vault.objects.root,
            o.builder.objects.root,
            o.qa.vault.objects.root,
            o.vault.objects.root,
        )

    def configuration(self, proposal, project):
        _, _, _, config, _ = self.workspaces._configuration(proposal, project)
        return digest(
            [
                config,
                project.preapproved,
                project.target_branch,
                self.operational.configurations[self.profile_name],
                self.quota.profiles[self.profile_name].model_dump(),
                [str(p) for p in self.roots(self.orchestrator)],
                self.orchestrator.preserve_budget,
            ]
        )

    @staticmethod
    def monetary(s, actor, run, proposal):
        proof = accounting(s, run, proposal, actor.owner_id)
        if not proof["valid"] or proof["status"] != "RESERVED" or proof["actual"] is not None:
            deny("formal_budget_reservation_required", 403)

    @staticmethod
    def policy(s, proposal, project):
        if project.preapproved:
            return
        queued = set(
            s.scalars(
                select(WorkflowEvent.aggregate_version).where(
                    WorkflowEvent.aggregate_id == proposal.id,
                    WorkflowEvent.owner_id == proposal.owner_id,
                    WorkflowEvent.event_type == "run.queued",
                )
            )
        )
        approvals = s.scalars(
            select(ApprovalRequest).where(ApprovalRequest.proposal_id == proposal.id)
        )
        if not any(
            a.action == "sandbox.execute"
            and a.decision == "APPROVED"
            and a.consumed
            and a.approver_id
            and a.actor_id == proposal.author_agent_id
            and a.evidence_hash == digest(proposal.document)
            and a.base_commit == proposal.document["prototype"]["base_commit"]
            and a.target_branch == project.target_branch
            and utc(a.expires_at) > now()
            and a.revision + 2 in queued
            for a in approvals
        ):
            deny("formal_execution_policy_not_authorized", 403)

    def change(self, s, actor, row, j, **values):
        new = j.model_copy(update={**values, "version": j.version + 1})
        self.save(s, actor, row.key, new, row)
        self.event(
            s,
            actor,
            j.run_id,
            "formal_" + new.state.lower(),
            version=new.version,
            dispatch_id=j.dispatch_id,
            generation=j.generation,
            code=new.error_code,
        )
        return new

    @guarded
    def execute(self, actor, run_id, expected_version, lease_token, request_key):
        if actor.role != "worker" or type(expected_version) is not int or expected_version < 1:
            deny("formal_worker_and_version_required", 403)
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 100:
            deny("formal_invalid_key", 403)
        hashed = digest([actor.id, request_key])[:40]
        namespace = "formal-dispatch.v1:" + hashed
        request_hash = digest(
            [run_id, expected_version, self.profile_name, token_hash(lease_token)]
        )
        with self.boundary.scope(actor, run_id, "control"):
            with self.factory.begin() as s:
                run, p, project = self.context(s, actor, run_id, lease_token)
                row = self.find(s, actor, namespace)
                if row:
                    j = self.value(row, FormalDispatch, actor.owner_id)
                    if (
                        j.actor_id != actor.id
                        or j.run_id != run_id
                        or j.request_sha256 != request_hash
                        or j.generation != run.lease_generation
                        or j.configuration_sha256 != self.configuration(p, project)
                    ):
                        deny("formal_request_conflict")
                    if j.state == "COMPLETE":
                        return j.result
                    # A concurrent duplicate must not invalidate the live winner.
                    # A restart also cannot replay this durable uncertain intent.
                    uncertain = True
                else:
                    self.context(s, actor, run_id, lease_token, live=True)
                    if p.version != expected_version:
                        deny("version_conflict")
                    self.policy(s, p, project)
                    self.monetary(s, actor, run, p)
                    others = s.scalars(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.owner_id == actor.owner_id,
                            IdempotencyRecord.key.like("formal-dispatch.v1:%"),
                            IdempotencyRecord.response["run_id"].as_string() == run_id,
                        )
                    )
                    if any(x.response.get("state") in {"DISPATCHING", "UNKNOWN"} for x in others):
                        deny("formal_other_dispatch_uncertain")
                    j = FormalDispatch(
                        dispatch_id=str(uuid4()),
                        run_id=run_id,
                        owner_id=actor.owner_id,
                        actor_id=actor.id,
                        generation=run.lease_generation,
                        expected_version=expected_version,
                        profile=self.profile_name,
                        request_sha256=request_hash,
                        configuration_sha256=self.configuration(p, project),
                    )
                    self.save(s, actor, namespace, j)
                    self.event(
                        s,
                        actor,
                        run_id,
                        "formal_dispatching",
                        version=1,
                        dispatch_id=j.dispatch_id,
                        generation=j.generation,
                    )
                    uncertain = False
        if uncertain:
            deny("formal_execution_uncertain")
        try:
            with self.boundary.scope(
                actor, run_id, "admission", generation=j.generation, dispatch_id=j.dispatch_id
            ):
                info = self.operational.run(
                    actor, run_id, "formal_" + hashed, lease_token, self.profile_name
                )
            result = {
                **info.model_dump(mode="json"),
                "id": run_id,
                "mode": "operational-deterministic",
                "verified": False,
            }
            with self.boundary.scope(actor, run_id, "control", generation=j.generation):
                with self.factory.begin() as s:
                    self.context(s, actor, run_id, lease_token)
                    row = self.find(s, actor, namespace)
                    current = self.value(row, FormalDispatch, actor.owner_id)
                    if current.state != "DISPATCHING" or current.version != j.version:
                        deny("formal_execution_uncertain")
                    self.change(s, actor, row, current, state="COMPLETE", result=result)
            return result
        except (DomainError, OSError, ValueError, TypeError) as error:
            code = error.code if isinstance(error, DomainError) else "formal_execution_uncertain"
            try:
                with self.boundary.scope(actor, run_id, "admission", dispatch_id=j.dispatch_id):
                    receipt = self.quota.receipt(actor, run_id, "formal_" + hashed, lease_token)
                state = "BLOCKED" if receipt is None or receipt.state == "RESERVED" else "UNKNOWN"
            except DomainError:
                state = "UNKNOWN"
            with self.boundary.scope(actor, run_id, "control"):
                with self.factory.begin() as s:
                    # Failure bookkeeping needs ownership/original credentials,
                    # not fresh repository read permission after grant revocation.
                    self.workspaces._authorize(s, actor, run_id, lease_token, cleanup=True)
                    row = self.find(s, actor, namespace)
                    current = self.value(row, FormalDispatch, actor.owner_id)
                    if current.state == "DISPATCHING":
                        self.change(s, actor, row, current, state=state, error_code=code)
            raise

    def initialize(self, actor, run_id):
        with self.boundary.scope(actor, run_id, "admin"):
            return self.quota.initialize(actor, run_id)

    def snapshot(self, actor, run_id):
        with self.boundary.scope(actor, run_id, "admin"):
            return self.health.snapshot(actor, run_id)

    def read_artifact(self, actor, run_id, artifact_id, token=None):
        with self.boundary.scope(actor, run_id, "inspect"):
            return self.orchestrator.store.read(actor, run_id, artifact_id, token)

    def review(self, actor, run_id, *args, **kwargs):
        with self.boundary.scope(actor, run_id, "inspect"):
            return self.orchestrator.review(actor, run_id, *args, **kwargs)

    def hold(self, actor, run_id, version, **kwargs):
        with self.boundary.scope(actor, run_id, "admin"):
            return self.retention.references.hold(actor, run_id, version, **kwargs)

    def cleanup_plan(self, actor, run_id, key):
        with self.boundary.scope(actor, run_id, "inspect"):
            return self.retention.plan(actor, run_id, key)

    def cleanup_approve(self, actor, run_id, identifier, version):
        with self.boundary.scope(actor, run_id, "inspect"):
            return self.retention.approve(actor, run_id, identifier, version)

    def cleanup_execute(self, actor, run_id, identifier, version):
        with self.boundary.scope(actor, run_id, "cleanup"):
            return self.retention.execute(actor, run_id, identifier, version)

    def release_quota(self, actor, run_id, identifier, version):
        with self.boundary.scope(actor, run_id, "admin"):
            return self.quota.release(actor, run_id, identifier, version)

    def stop_cancelled(self, actor, proposal_id):
        with self.factory() as s:
            run = s.scalar(select(PrototypeRun).where(PrototypeRun.proposal_id == proposal_id))
            if not run:
                return None
            identifier = run.id

        def hook():
            with self.boundary.scope(actor, identifier, "stop"):
                with self.factory.begin() as s:
                    r, _, _ = self.workspaces._authorize(s, actor, identifier, None, cleanup=True)
                    sandbox = s.scalar(
                        select(SandboxInstance).where(SandboxInstance.run_id == r.id)
                    )
                    jobs = [
                        QAJournal.model_validate(x.response)
                        for x in s.scalars(
                            select(IdempotencyRecord).where(
                                IdempotencyRecord.owner_id == actor.owner_id,
                                IdempotencyRecord.key.like("qa-runner.v1:%"),
                            )
                        )
                        if x.response.get("run_id") == identifier
                    ]
                if sandbox:
                    self.orchestrator.controller.stop(actor, identifier, None)
                driver = self.orchestrator.qa.driver
                for j in jobs:
                    if not hasattr(driver, "job_info"):
                        continue
                    row = self.orchestrator.qa._resource(j)
                    profile = self.orchestrator.qa.registry.get(j.profile_name)
                    info = driver.job_info(row)
                    if info:
                        driver.verify_job(row, profile)
                        mark = self.boundary.depth.set(self.boundary.depth.get() + 1)
                        try:
                            driver.call("container", "stop", "--time", "1", info["Id"])
                        finally:
                            self.boundary.depth.reset(mark)

        return DurableLeaseManager(self.factory).stop(actor, identifier, hook).stop_state
