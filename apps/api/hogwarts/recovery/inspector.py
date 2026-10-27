"""Read-only business inspection under Run-first lock; never promotes evidence."""

import hashlib
from datetime import timedelta

from sqlalchemy import func, or_, select

from hogwarts.models import (
    ApprovalRequest,
    ArtifactMetadata,
    GitWorkspace,
    IdempotencyRecord,
    OutputImport,
    SandboxInstance,
    WorkflowEvent,
    now,
)
from hogwarts.outputs.archive import validate_archive
from hogwarts.patches.contracts import BuildJournal
from hogwarts.qa.contracts import QAJournal
from hogwarts.reconciliation.inventory import ResourceInventory
from hogwarts.recovery.accounting import accounting
from hogwarts.recovery.classifier import RecoveryClassifier
from hogwarts.recovery.contracts import Observation, deny
from hogwarts.service import DomainError, digest, utc
from hogwarts.verification.contracts import ReviewBundle, VerificationJournal
from hogwarts.verification.profiles import approved_review_profile
from hogwarts.workspaces.git import plain
from hogwarts.workspaces.manager import token_hash


class WorkflowStateInspector:
    def __init__(self, orchestrator, backend=None, stale_seconds=60):
        if type(stale_seconds) is not int or not 0 <= stale_seconds <= 86400:
            raise ValueError("invalid stale interval")
        self.o, self.workspaces = orchestrator, orchestrator.workspaces
        self.inventory = ResourceInventory(self.workspaces, backend)
        self.stale_seconds = stale_seconds

    def context(self, s, actor, run_id, token):
        run, proposal, project = self.workspaces._authorize(s, actor, run_id, token, cleanup=True)
        repo, allowed, base, configuration, _ = self.workspaces._configuration(proposal, project)
        return run, proposal, project, configuration

    def inspect(self, s, actor, run_id, token=None, *, lock_budget=False):
        run, p, project, configuration = self.context(s, actor, run_id, token)
        if actor.role == "worker":
            # Store worker reads require a live claimed Run. Cleanup credentials
            # authorize stop/receipt operations, never fresh historical byte reads.
            self.workspaces._authorize(s, actor, run_id, token)
        unavailable = False
        try:
            resource_hash, resources = self.inventory.inspect(s, run, p, project)
        except (DomainError, OSError, ValueError, TypeError):
            resource_hash, resources = digest("resource_observation_unavailable"), ()
            unavailable = True
        budget = accounting(s, run, p, actor.owner_id, lock=lock_budget)
        facts = {
            "problem": "recovery_resources_unavailable" if unavailable else None,
            "budget": budget,
            "candidate": None,
            "terminal": False,
            "cancel_normalize": False,
            "stale_approval": False,
            "cost_proof": False,
            "resources_present": any(
                x.kind in {"container", "volume", "worktree"} for x in resources
            ),
            "unknown_execution": False,
            "active": (
                run.status == "CLAIMED"
                and p.status == "QUEUED"
                and run.lease_generation == run.lease_issued_generation > 0
                and run.lease_revoked_at is None
                and run.lease_expires_at is not None
                and utc(run.lease_expires_at) > now()
            ),
        }
        if any(x.disposition == "QUARANTINE" for x in resources):
            facts["problem"] = "recovery_resource_identity_uncertain"
        artifacts = list(
            s.scalars(select(ArtifactMetadata).where(ArtifactMetadata.run_id == run_id))
        )
        artifacts.sort(key=lambda x: x.id)
        hashes, artifact_rows = {}, {}
        for row in artifacts:
            if (
                row.owner_id != actor.owner_id
                or row.producer_id != run.worker_id
                or row.lease_hash != token_hash(run.lease_token or "")
            ):
                facts["problem"] = "recovery_artifact_binding_mismatch"
                continue
            artifact_rows[row.id] = row
            try:
                self.o.store._metadata(row)
                if row.state in {"FAILED", "CORRUPT"}:
                    deny("recovery_artifact_corrupt")
                # PENDING content is not readable through Store.read either.
                # Its originating capsule can authorize an exact put retry; only
                # that publisher may validate/finalize the unpublished object.
                if row.state == "READY":
                    data = self.o.store.objects.read(row.id, row.sha256, row.size_bytes)
                    hashes[row.id] = hashlib.sha256(data).hexdigest()
            except (DomainError, OSError, ValueError):
                facts["problem"] = "recovery_artifact_integrity_failed"
        imports = list(s.scalars(select(OutputImport).where(OutputImport.run_id == run_id)))
        imports.sort(key=lambda x: x.id)
        rows = list(
            s.scalars(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.owner_id == actor.owner_id,
                    IdempotencyRecord.actor_id == run.worker_id,
                )
            )
        )
        rows = [
            x
            for x in rows
            if x.key.startswith(
                ("patch-builder.v1:", "qa-runner.v1:", "prototype-verification.v1:")
            )
            and x.response.get("run_id") == run_id
        ]
        capsule_proofs = []
        qa_states, builds = {}, {}
        try:
            for imp in imports:
                if (
                    imp.producer_id != run.worker_id
                    or imp.lease_hash != token_hash(run.lease_token or "")
                    or imp.owner_id != actor.owner_id
                ):
                    deny("recovery_import_binding_mismatch")
                if imp.state in {"SNAPSHOTTING", "SNAPSHOT_READY", "IMPORTING", "IMPORTED"}:
                    if self.o.importer.vault.exists(imp):
                        raw, proof = self.o.importer.vault.open(imp)
                        sb = s.get(SandboxInstance, imp.sandbox_id)
                        grant = self.o.importer.grants[sb.profile_name]
                        ws = s.get(GitWorkspace, imp.workspace_id)
                        if (
                            sb.run_id != run_id
                            or ws.run_id != run_id
                            or proof.get("container_id") != sb.container_id
                            or proof.get("invocation_id") != sb.invocation_id
                            or proof.get("profile_hash") != sb.request_hash
                            or proof.get("sandbox_input_sha256") != sb.snapshot_sha256
                            or proof.get("reader_image") != self.o.controller.driver.reader_image
                        ):
                            deny("recovery_import_proof_changed")
                        validate_archive(raw, grant.target_prefix, tuple(ws.allowed_paths))
                        capsule_proofs.append(hashlib.sha256(raw).hexdigest())
                        if imp.state != "IMPORTED":
                            facts["candidate"] = ("IMPORT", imp.id)
                    else:
                        facts["unknown_execution"] = True
            for row in sorted(rows, key=lambda x: x.key):
                if row.key.startswith("patch-builder.v1:"):
                    j = BuildJournal.model_validate(row.response)
                    builds[j.build_id] = j
                    if j.build_id != row.id or j.run_id != run_id:
                        deny("recovery_patch_binding_mismatch")
                    if j.state in {"BUILT", "PUBLISHING"}:
                        size = plain(self.o.builder.objects.path(j.build_id)).st_size
                        raw = self.o.builder.objects.read(j.build_id, j.patch_sha256, size)
                        if not j.proof or digest(j.proof.model_dump(mode="json")) != j.proof_sha256:
                            deny("recovery_patch_proof_changed")
                        capsule_proofs.append(hashlib.sha256(raw).hexdigest())
                        facts["candidate"] = ("PATCH", j.build_id)
                elif row.key.startswith("qa-runner.v1:"):
                    j = QAJournal.model_validate(row.response)
                    profile = self.o.qa.registry.get(j.profile_name)
                    if (
                        j.qa_id != row.id
                        or j.worker_id != run.worker_id
                        or j.run_id != run_id
                        or j.controller_id != self.o.qa.driver.controller_id
                        or j.lease_hash != token_hash(run.lease_token or "")
                        or j.profile_sha256 != profile.fingerprint
                    ):
                        deny("recovery_qa_binding_mismatch")
                    if self.o.qa.vault.exists(self.o.qa._capsule_row(j)):
                        raw, result = self.o.qa._result(j)
                        if result.image_id != profile.image:
                            deny("recovery_qa_image_changed")
                        qa_states[j.qa_id] = (j, result)
                        capsule_proofs.append(hashlib.sha256(raw).hexdigest())
                        if result.outcome == "unknown_execution" or result.exit_code is None:
                            facts["unknown_execution"] = True
                        elif j.state in {"EXECUTING", "CAPTURED", "PUBLISHING"}:
                            facts["candidate"] = ("QA", j.qa_id)
                    elif j.state in {"EXECUTING", "CAPTURED", "PUBLISHING", "READY"}:
                        facts["unknown_execution"] = True
            for row in rows:
                if not row.key.startswith("prototype-verification.v1:"):
                    continue
                j = VerificationJournal.model_validate(row.response)
                if (
                    j.verification_id != row.id
                    or j.worker_id != run.worker_id
                    or j.run_id != run_id
                    or j.lease_hash != token_hash(run.lease_token or "")
                ):
                    deny("recovery_verification_binding_mismatch")
                if self.o.vault.exists(self.o._capsule(j)):
                    raw, proof = self.o.vault.open(self.o._capsule(j))
                    b = ReviewBundle.model_validate_json(raw)
                    profile = approved_review_profile(self.o.qa.registry.get(b.profile_name))
                    if (
                        proof != {"issuer": "prototype-verification.v1"}
                        or b.run_id != run_id
                        or b.verification_id != j.verification_id
                        or b.worker_id != run.worker_id
                        or b.configuration_sha256 != configuration
                        or b.proposal_sha256 != digest(p.document)
                        or b.profile_sha256 != profile.fingerprint
                        or b.image_id != profile.image
                    ):
                        deny("recovery_review_bundle_changed")
                    for ref in b.artifacts:
                        meta = artifact_rows.get(ref.artifact_id)
                        if (
                            not meta
                            or meta.state != "READY"
                            or hashes.get(meta.id) != ref.sha256
                            or meta.size_bytes != ref.size_bytes
                            or meta.workspace_id != b.workspace_id
                            or meta.sandbox_id != b.sandbox_id
                        ):
                            deny("recovery_review_evidence_missing")
                    capsule_proofs.append(hashlib.sha256(raw).hexdigest())
                    q = qa_states.get(b.qa_id)
                    built = builds.get(b.build_id)
                    if q and (
                        q[0].build_id != b.build_id
                        or q[0].workspace_id != b.workspace_id
                        or q[0].sandbox_id != b.sandbox_id
                        or q[0].profile_name != b.profile_name
                        or q[0].profile_sha256 != b.profile_sha256
                        or q[0].input_sha256 != b.input_sha256
                        or q[1].image_id != b.image_id
                        or q[0].attestation_artifact_id != b.attestation_artifact_id
                        or hashes.get(b.attestation_artifact_id) != q[0].attestation_sha256
                    ):
                        deny("recovery_review_qa_mismatch")
                    if (
                        not built
                        or built.state != "READY"
                        or not built.proof
                        or built.patch_artifact_id != b.patch_artifact_id
                        or built.proof_artifact_id != b.build_proof_artifact_id
                        or built.proof.result_tree != b.result_tree
                        or hashes.get(b.patch_artifact_id) != built.patch_sha256
                        or hashes.get(b.build_proof_artifact_id) != built.proof_sha256
                    ):
                        deny("recovery_review_patch_mismatch")
                    if not q or q[1].outcome == "unknown_execution":
                        facts["unknown_execution"] = True
                    if j.state in {"EXECUTING", "CAPTURED", "PUBLISHING"}:
                        facts["candidate"] = ("VERIFICATION", j.verification_id)
                    if j.state == "COMPLETE":
                        ev = run.evidence or {}
                        if (
                            not j.bundle_artifact_id
                            or hashes.get(j.bundle_artifact_id) != j.bundle_sha256
                            or j.bundle_sha256 != hashlib.sha256(raw).hexdigest()
                            or ev.get("verification_id") != j.verification_id
                            or ev.get("bundle_artifact_id") != j.bundle_artifact_id
                            or ev.get("bundle_sha256") != j.bundle_sha256
                            or b.verdict != j.verdict
                        ):
                            deny("recovery_terminal_evidence_mismatch")
                        facts["terminal"] = (
                            run.status == "READY_FOR_HUMAN_REVIEW"
                            and p.status in {"REVIEWING", "ACCEPTED", "REJECTED", "DEFERRED"}
                        ) or (run.status in {"FAILED", "BLOCKED"} and p.status == "FAILED")
                        facts["cost_proof"] = bool(
                            q
                            and q[1].outcome in {"pass", "fail", "timeout"}
                            and q[1].exit_code is not None
                            and b.verdict in {"READY_FOR_HUMAN_REVIEW", "FAILED"}
                        )
                        if p.status == "REVIEWING":
                            approval = (
                                s.get(ApprovalRequest, j.approval_id) if j.approval_id else None
                            )
                            facts["stale_approval"] = not approval or (
                                approval.decision != "PENDING"
                                or approval.proposal_id != p.id
                                or approval.action != "prototype.review.bundle"
                                or approval.base_commit != b.base_commit
                                or approval.target_branch != project.target_branch
                                or approval.actor_id != p.author_agent_id
                                or utc(approval.expires_at) <= now()
                                or approval.evidence_hash != j.bundle_sha256
                                or approval.revision != p.version
                            )
                elif (
                    j.state in {"COMPLETE", "UNKNOWN", "BLOCKED", "EXECUTING"}
                    and not facts["candidate"]
                ):
                    facts["unknown_execution"] = True
            sb = s.scalar(select(SandboxInstance).where(SandboxInstance.run_id == run_id))
            if sb and sb.state == "EXECUTING" and not sb.result:
                facts["unknown_execution"] = True
            cancelled = p.status == "CANCELLED" and run.lease_revoked_at is not None
            if cancelled:
                event = s.scalar(
                    select(WorkflowEvent.id).where(
                        WorkflowEvent.aggregate_id == p.id,
                        WorkflowEvent.event_type == "proposal.cancelled",
                        WorkflowEvent.source == "governance",
                    )
                )
                if event:
                    facts["cancel_normalize"] = run.status != "CANCELLED"
                    facts["terminal"] = run.status == "CANCELLED"
                else:
                    facts["problem"] = "recovery_cancel_proof_missing"
            if (
                run.status == "COMPLETED"
                and (run.evidence or {}).get("mode") == "fake"
                and p.status in {"AWAITING_HUMAN_REVIEW", "ACCEPTED", "REJECTED", "DEFERRED"}
            ):
                event = s.scalar(
                    select(WorkflowEvent.id).where(
                        WorkflowEvent.aggregate_id == p.id,
                        WorkflowEvent.event_type == "prototype.presented",
                        WorkflowEvent.source == "governance",
                    )
                )
                facts["terminal"], facts["cost_proof"] = bool(event), bool(event)
                if event:
                    capsule_proofs.append(digest({"m1_fake_completion_event": event}))
            if (
                run.status == "FAILED"
                and p.status == "FAILED"
                and (run.evidence or {}).get("mode") == "fake"
                and run.evidence.get("error") == "adapter_failure"
            ):
                event = s.scalar(
                    select(WorkflowEvent.id).where(
                        WorkflowEvent.aggregate_id == p.id,
                        WorkflowEvent.owner_id == actor.owner_id,
                        WorkflowEvent.event_type == "run.failed",
                        WorkflowEvent.source == "governance",
                    )
                )
                facts["terminal"], facts["cost_proof"] = bool(event), bool(event)
                if event:
                    capsule_proofs.append(digest({"m1_fake_failure_event": event}))
                if run.evidence.get("cleanup_failed"):
                    facts["problem"] = "recovery_fake_cleanup_uncertain"
        except (DomainError, OSError, ValueError, KeyError, TypeError):
            facts["problem"] = "recovery_durable_proof_invalid"
        if facts["unknown_execution"]:
            facts["terminal"] = facts["cost_proof"] = False
        activity = s.scalar(
            select(func.max(WorkflowEvent.occurred_at)).where(
                or_(WorkflowEvent.aggregate_id == run_id, WorkflowEvent.aggregate_id == p.id),
                WorkflowEvent.source != "workflow_recovery",
            )
        )
        stamp = activity or run.started_at or p.created_at
        stalled = (
            run.lease_revoked_at is not None
            or (run.lease_expires_at is not None and utc(run.lease_expires_at) <= now())
            or utc(stamp) <= now() - timedelta(seconds=self.stale_seconds)
            or facts["problem"] is not None
            or facts["cancel_normalize"]
            or (facts["terminal"] and budget["status"] == "RESERVED")
        )
        components = {
            "resource": resource_hash,
            "budget": budget,
            "run": [run.status, run.evidence],
            "proposal": [p.status, p.version],
            "artifacts": [
                [x.id, x.state, x.version, x.sha256, x.metadata_sha256] for x in artifacts
            ],
            "journals": sorted([x.id, digest(x.response)] for x in rows),
            "imports": [[x.id, x.state, x.version] for x in imports],
            "capsules": sorted(capsule_proofs),
        }
        binding = digest(components)
        proof = (
            digest(
                {
                    "protocol": "deterministic-local-zero-cost.v1",
                    "run_id": run_id,
                    "reservation_id": budget["reservation"],
                    "amount": budget["amount"],
                    "actual_cents": 0,
                    "capsules": sorted(capsule_proofs),
                }
            )
            if facts["cost_proof"]
            else None
        )
        return Observation(
            run_id=run.id,
            project_id=project.id,
            generation=run.lease_generation,
            lease_version=run.lease_version,
            fingerprint=binding,
            stalled=stalled,
            decision=RecoveryClassifier.classify(facts, actor),
            artifact_hashes=hashes,
            proof_sha256=proof,
            component_hashes={k: digest(v) for k, v in components.items()},
        ), facts
