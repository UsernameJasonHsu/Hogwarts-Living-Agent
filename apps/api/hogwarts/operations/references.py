"""Permanent evidence is never a deletion candidate, even without live references."""

from sqlalchemy import or_, select

from hogwarts.artifacts.store import metadata_hash
from hogwarts.models import (
    ApprovalRequest,
    ArtifactMetadata,
    IdempotencyRecord,
    MergeRequest,
    OutputImport,
)
from hogwarts.operations.contracts import EvidenceHold
from hogwarts.operations.state import OperationalState, guarded
from hogwarts.reconciliation.contracts import deny
from hogwarts.service import DomainError, digest
from hogwarts.workspaces.manager import token_hash


class ReferenceProtector(OperationalState):
    def __init__(self, workspaces, store=None):
        super().__init__(workspaces)
        if store and store.workspaces is not workspaces:
            deny("retention_store_binding_mismatch", 403)
        self.store = store

    @guarded
    def hold(self, actor, run_id, expected_version, *, active=True, reason="operator"):
        with self.factory.begin() as s:
            self.context(s, actor, run_id, admin=True)
            namespace = "evidence-hold.v1:" + run_id
            row = self.find(s, actor, namespace, lock=True)
            prior = self.value(row, EvidenceHold, actor.owner_id) if row else None
            if type(expected_version) is not int or expected_version != (
                prior.version if prior else 0
            ):
                deny("retention_hold_cas_conflict")
            value = EvidenceHold(
                run_id=run_id,
                owner_id=actor.owner_id,
                active=active,
                reason=reason,
                version=expected_version + 1,
            )
            self.save(s, actor, namespace, value, row)
            self.event(
                s,
                actor,
                run_id,
                "hold_changed",
                version=value.version,
                active=active,
                reason=reason,
            )
            return value

    def inspect(self, s, actor, run, proposal):
        if (
            actor.role != "human"
            or proposal.owner_id != actor.owner_id
            or run.proposal_id != proposal.id
        ):
            deny("operations_owner_human_required", 403)
        reasons = set()
        row = self.find(s, actor, "evidence-hold.v1:" + run.id)
        hold = self.value(row, EvidenceHold, actor.owner_id) if row else None
        if hold and hold.active:
            reasons.add("retention_explicit_hold")
        if proposal.status in {"REVIEWING", "DEFERRED"}:
            reasons.add("retention_active_review")
        if run.status == "UNKNOWN":
            reasons.add("retention_unknown_execution")
        artifacts = list(
            s.scalars(
                select(ArtifactMetadata).where(ArtifactMetadata.run_id == run.id).limit(10001)
            )
        )
        if len(artifacts) > 10000:
            deny("retention_inventory_limit")
        snapshot = {
            "run": [run.status, run.evidence],
            "proposal": [proposal.status, proposal.version, proposal.document],
            "hold": hold.model_dump() if hold else None,
            "artifacts": [],
            "imports": [],
            "journals": [],
            "approvals": [],
            "merge": [],
        }
        known = {x.id for x in artifacts if x.owner_id == actor.owner_id}
        references = set()

        def refs(value):
            if isinstance(value, dict):
                for name, v in value.items():
                    if name.endswith("artifact_id") and v is not None:
                        if isinstance(v, str):
                            references.add(v)
                        else:
                            reasons.add("retention_reference_unresolved")
                    refs(v)
            elif isinstance(value, list):
                for v in value:
                    refs(v)

        refs(run.evidence)
        for a in artifacts:
            if (
                a.owner_id != actor.owner_id
                or a.producer_id != run.worker_id
                or a.lease_hash != token_hash(run.lease_token or "")
                or metadata_hash(a) != a.metadata_sha256
            ):
                reasons.add("retention_evidence_identity_uncertain")
            elif not self.store:
                reasons.add("retention_artifact_verifier_required")
            elif a.state != "READY":
                reasons.add("retention_artifact_storage_unknown")
            else:
                try:
                    # Caller is owner-human under the existing Run lock. Pure
                    # helpers avoid nested transactions or evidence mutation.
                    self.store._metadata(a)
                    self.store.objects.read(a.id, a.sha256, a.size_bytes)
                except (DomainError, OSError, ValueError):
                    reasons.add("retention_artifact_storage_unknown")
            snapshot["artifacts"].append([a.id, a.sha256, a.metadata_sha256, a.state, a.version])
        for i in s.scalars(select(OutputImport).where(OutputImport.run_id == run.id)):
            if i.owner_id != actor.owner_id:
                reasons.add("retention_evidence_identity_uncertain")
            if i.state in {"SNAPSHOTTING", "UNCERTAIN", "IMPORTING"}:
                reasons.add("retention_unknown_execution")
            if i.manifest_artifact_id:
                references.add(i.manifest_artifact_id)
            snapshot["imports"].append(
                [i.id, i.state, i.version, i.manifest_artifact_id, i.archive_sha256]
            )
        journals = list(
            s.scalars(
                select(IdempotencyRecord)
                .where(
                    IdempotencyRecord.owner_id == actor.owner_id,
                    or_(
                        IdempotencyRecord.response["run_id"].as_string() == run.id,
                        IdempotencyRecord.response["observation"]["run_id"].as_string() == run.id,
                    ),
                )
                .limit(10001)
            )
        )
        if len(journals) > 10000:
            deny("retention_inventory_limit")
        for j in journals:
            if j.actor_id == "operations.v1" or j.key.startswith("resource-cleanup.v1:"):
                continue
            data = j.response
            nested = data.get("observation", {})
            if data.get("run_id") != run.id and nested.get("run_id") != run.id:
                continue
            if (
                data.get("state") in {"EXECUTING", "UNKNOWN", "UNCERTAIN", "PUBLISHING"}
                or data.get("outcome") == "unknown_execution"
                or data.get("verdict") == "UNKNOWN"
            ):
                reasons.add("retention_unknown_execution")
            refs(data)
            snapshot["journals"].append([j.id, digest(data)])
        for a in s.scalars(
            select(ApprovalRequest).where(ApprovalRequest.proposal_id == proposal.id)
        ):
            if a.action == "prototype.review.bundle" and a.decision == "PENDING":
                reasons.add("retention_active_review")
            snapshot["approvals"].append(
                [
                    a.id,
                    a.action,
                    a.decision,
                    a.consumed,
                    a.revision,
                    a.evidence_hash,
                    str(a.expires_at),
                ]
            )
        for m in s.scalars(select(MergeRequest).where(MergeRequest.proposal_id == proposal.id)):
            snapshot["merge"].append([m.id, m.status, m.approval_id])
        if references - known:
            reasons.add("retention_reference_unresolved")
        for k in ("artifacts", "imports", "journals", "approvals", "merge"):
            snapshot[k].sort()
        return digest(snapshot), tuple(sorted(reasons))
