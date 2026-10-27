"""At-most-once pipeline, immutable review publication, separate human merge intent."""

import hashlib
import secrets
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from hogwarts.artifacts.contracts import ArtifactUpload
from hogwarts.models import (
    ApprovalRequest,
    IdempotencyRecord,
    MergeRequest,
    Project,
    PrototypeRun,
    WorkflowEvent,
    now,
)
from hogwarts.outputs.capsule import CapsuleVault, canonical
from hogwarts.schemas import HumanDecision, State
from hogwarts.service import (
    DomainError,
    cas,
    command,
    decision,
    digest,
    proposal_for,
    release,
    require_human,
    utc,
)
from hogwarts.verification.contracts import ReviewBundle, VerificationInfo, VerificationJournal
from hogwarts.verification.gate import AcceptanceGate
from hogwarts.verification.profiles import approved_review_profile
from hogwarts.workspaces.manager import token_hash


def deny(code, status=409):
    raise DomainError(status, code)


class LocalPrototypeVerificationOrchestrator:
    def __init__(self, qa, root, *, preserve_budget=False):
        # Legacy pure-fixture composition retains its zero-cost settlement.
        # Formal operational composition must keep commitments until actual proof.
        self.preserve_budget = preserve_budget
        self.qa, self.builder, self.importer = qa, qa.builder, qa.builder.importer
        self.controller, self.store, self.workspaces = (
            self.importer.controller,
            qa.store,
            qa.workspaces,
        )
        self.factory, self.gate = qa.factory, AcceptanceGate(qa)
        self.vault = CapsuleVault(
            root,
            (
                qa.vault.objects.root,
                self.builder.objects.root,
                self.store.objects.root,
                self.importer.vault.objects.root,
                self.workspaces.root,
                *(g.repository for g in self.workspaces.grants.values()),
            ),
        )

    def _key(self, run_id):
        return "prototype-verification.v1:" + run_id

    def _row(self, s, actor, run_id):
        row = s.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == actor.owner_id,
                IdempotencyRecord.actor_id == actor.id,
                IdempotencyRecord.key == self._key(run_id),
            )
        )
        if row:
            s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id)
                .values(
                    request_hash=IdempotencyRecord.request_hash,
                )
            )
            s.refresh(row)
        return row

    def _event(self, s, actor, run_id, event, j=None, code=None):
        s.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=j.version if j else 0,
                event_type="verification." + event,
                source="prototype_verification",
                actor_id=actor.id,
                reason=event,
                payload={"verification_id": j.verification_id if j else None, "code": code},
            )
        )

    def _state(self, s, actor, row, j, **changes):
        result = VerificationJournal.model_validate(
            {
                **j.model_dump(mode="json"),
                **changes,
                "version": j.version + 1,
            }
        )
        row.response = result.model_dump(mode="json")
        self._event(s, actor, j.run_id, result.state.lower(), result, result.error_code)
        return result

    def _capsule(self, j):
        return SimpleNamespace(
            id=j.verification_id,
            run_id=j.run_id,
            workspace_id=j.workspace_id,
            sandbox_id=j.sandbox_id,
            request_hash=j.request_hash,
            seal_key=j.seal_key,
        )

    def _info(self, j):
        return VerificationInfo(
            verification_id=j.verification_id,
            run_id=j.run_id,
            status=j.verdict or "UNKNOWN",
            bundle_artifact_id=j.bundle_artifact_id,
            bundle_sha256=j.bundle_sha256,
            approval_id=j.approval_id,
            proposal_version=j.proposal_version,
        )

    def _locked_evidence(self, session, actor, j, bundle, token=None):
        """Reuse Store authorization/FD checks within the final business transaction."""
        for ref in bundle.artifacts:
            row = self.store._get(session, actor, j.run_id, ref.artifact_id, token)
            self.store._metadata(row)
            if row.sha256 != ref.sha256 or row.size_bytes != ref.size_bytes:
                deny("review_evidence_changed")
            self.store.objects.read(row.id, ref.sha256, ref.size_bytes)

    def verify(self, actor, run_id, lease_token, runtime_profile, qa_profile):
        ownership = {"execute": False}
        try:
            return self._verify(actor, run_id, lease_token, runtime_profile, qa_profile, ownership)
        except (DomainError, SQLAlchemyError, OSError, ValueError, TypeError) as error:
            code = (
                error.code
                if isinstance(error, DomainError)
                else "verification_infrastructure_error"
            )
            try:
                with self.factory.begin() as s:
                    self._event(s, actor, run_id, "denied", code=code)
                    row = self._row(s, actor, run_id)
                    if row and ownership["execute"]:
                        j = VerificationJournal.model_validate(row.response)
                        if j.state == "EXECUTING" and not self.vault.exists(self._capsule(j)):
                            # No recovery execution: preserve uncertainty/denial as durable facts.
                            blocked = (
                                isinstance(error, DomainError)
                                and error.status < 500
                                and not any(
                                    word in error.code for word in ("unknown", "uncertain", "busy")
                                )
                            )
                            self._state(
                                s,
                                actor,
                                row,
                                j,
                                state="BLOCKED" if blocked else "UNKNOWN",
                                verdict="BLOCKED" if blocked else "UNKNOWN",
                                error_code=code,
                            )
            except SQLAlchemyError:
                raise DomainError(503, "verification_database_and_audit_unavailable") from None
            raise DomainError(
                error.status if isinstance(error, DomainError) else 503, code
            ) from None

    def resume_publication(self, actor, run_id, token, runtime_profile, qa_profile):
        """Recover only an authenticated ReviewBundle; never run the pipeline."""
        return self._verify(
            actor,
            run_id,
            token,
            runtime_profile,
            qa_profile,
            {"execute": False},
            publication_only=True,
        )

    def _verify(
        self,
        actor,
        run_id,
        token,
        runtime_profile,
        qa_profile,
        ownership,
        *,
        publication_only=False,
    ):
        profile = approved_review_profile(self.qa.registry.get(qa_profile))
        if runtime_profile not in self.controller.profiles:
            deny("verification_runtime_profile_not_approved", 403)
        with self.factory() as s:
            row = s.scalar(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.owner_id == actor.owner_id,
                    IdempotencyRecord.actor_id == actor.id,
                    IdempotencyRecord.key == self._key(run_id),
                )
            )
            existing = VerificationJournal.model_validate(row.response) if row else None
            if publication_only and (
                existing is None
                or existing.state not in {"EXECUTING", "CAPTURED", "PUBLISHING", "COMPLETE"}
                or not self.vault.exists(self._capsule(existing))
            ):
                deny("verification_publication_proof_required")
            _, proposal, project = self.workspaces._authorize(
                s,
                actor,
                run_id,
                token,
                cleanup=bool(existing and existing.state == "COMPLETE"),
            )
            _, _, _, configuration, _ = self.workspaces._configuration(proposal, project)
            request_hash = digest(
                {
                    "run": run_id,
                    "configuration": configuration,
                    "runtime": asdict(self.controller.profiles[runtime_profile]),
                    "qa": profile.fingerprint,
                    "lease": token_hash(token),
                }
            )
            if existing:
                if existing.request_hash != request_hash or existing.lease_hash != token_hash(
                    token
                ):
                    deny("verification_request_changed")
                if proposal.status == State.CANCELLED:
                    deny("verification_cancelled", 403)
                if existing.state == "COMPLETE":
                    return self._info(existing)
        try:
            with self.factory.begin() as s:
                row = self._row(s, actor, run_id)
                if not row:
                    j = VerificationJournal(
                        verification_id=str(uuid4()),
                        run_id=run_id,
                        worker_id=actor.id,
                        lease_hash=token_hash(token),
                        request_hash=request_hash,
                        runtime_profile=runtime_profile,
                        qa_profile=qa_profile,
                        profile_sha256=profile.fingerprint,
                        seal_key=secrets.token_hex(32),
                    )
                    s.add(
                        IdempotencyRecord(
                            id=j.verification_id,
                            owner_id=actor.owner_id,
                            actor_id=actor.id,
                            key=self._key(run_id),
                            request_hash=request_hash,
                            response=j.model_dump(mode="json"),
                        )
                    )
                    self._event(s, actor, run_id, "pending", j)
        except IntegrityError:
            pass
        execute = False
        with self.factory.begin() as s:
            self.workspaces._authorize(s, actor, run_id, token)
            row = self._row(s, actor, run_id)
            j = VerificationJournal.model_validate(row.response)
            if row.request_hash != request_hash or j.request_hash != request_hash:
                deny("verification_request_changed")
            if j.state == "PENDING":
                if publication_only:
                    deny("verification_recovery_execution_forbidden")
                self.workspaces._authorize(s, actor, run_id, token)
                j = self._state(s, actor, row, j, state="EXECUTING")
                execute = True
                ownership["execute"] = True
            elif j.state not in {"EXECUTING", "CAPTURED", "PUBLISHING"}:
                deny("verification_busy_or_unknown")
            elif not self.vault.exists(self._capsule(j)):
                deny("verification_busy_or_unknown")
        if execute:
            if publication_only:
                deny("verification_recovery_execution_forbidden")
            ws = self.workspaces.create(actor, run_id, token)
            sb = self.controller.create(actor, run_id, ws.id, token, runtime_profile)
            with self.factory.begin() as s:
                self.workspaces._authorize(s, actor, run_id, token)
                row = self._row(s, actor, run_id)
                j = self._state(s, actor, row, j, workspace_id=ws.id, sandbox_id=sb.id)
            self.controller.start(actor, run_id, token)
            self.controller.execute(actor, run_id, token, "verification-" + j.verification_id)
            imported = self.controller.import_outputs(
                self.importer, actor, run_id, ws.id, sb.id, token
            )
            built = self.builder.build(actor, run_id, imported.id, token)
            result = self.qa.run(actor, run_id, built.build_id, qa_profile, token)
            bundle = self.gate.evaluate(actor, run_id, token, j.verification_id, built, result)
            self.controller.destroy(actor, run_id, token)
            self.vault.seal(
                self._capsule(j),
                canonical(bundle.model_dump(mode="json")),
                {"issuer": "prototype-verification.v1"},
            )
        raw, proof = self.vault.open(self._capsule(j))
        bundle = ReviewBundle.model_validate_json(raw)
        if proof != {"issuer": "prototype-verification.v1"} or (
            bundle.verification_id != j.verification_id
            or bundle.run_id != run_id
            or bundle.worker_id != actor.id
            or bundle.profile_sha256 != profile.fingerprint
        ):
            deny("verification_capsule_binding_mismatch")
        # Publication-only retry: no Git/sandbox execution is reached here.
        for ref in bundle.artifacts:
            obj = self.store.read(actor, run_id, ref.artifact_id, token)
            if (
                hashlib.sha256(obj.content).hexdigest() != ref.sha256
                or len(obj.content) != ref.size_bytes
            ):
                deny("verification_artifact_changed")
        with self.factory.begin() as s:
            self.workspaces._authorize(s, actor, run_id, token)
            row = self._row(s, actor, run_id)
            current = VerificationJournal.model_validate(row.response)
            j = self._state(s, actor, row, current, state="PUBLISHING")
        obj = self.store.put(
            actor,
            run_id,
            j.workspace_id,
            j.sandbox_id,
            token,
            "vb_" + j.verification_id.replace("-", ""),
            ArtifactUpload(kind="report", media_type="application/json"),
            raw,
        ).artifact
        with self.factory.begin() as s:
            self.workspaces._authorize(s, actor, run_id, token, cleanup=True)
            row = self._row(s, actor, run_id)
            current = VerificationJournal.model_validate(row.response)
            if current.state == "COMPLETE":
                return self._info(current)
            run, proposal, project = self.workspaces._authorize(s, actor, run_id, token)
            _, _, _, fingerprint, _ = self.workspaces._configuration(proposal, project)
            if (
                fingerprint != bundle.configuration_sha256
                or digest(proposal.document) != bundle.proposal_sha256
            ):
                deny("verification_configuration_changed", 403)
            self._locked_evidence(s, actor, j, bundle, token)
            approval = None
            if bundle.verdict == "READY_FOR_HUMAN_REVIEW":
                for status in (
                    State.SANDBOX_READY,
                    State.IMPLEMENTING,
                    State.TESTING,
                    State.REVIEWING,
                ):
                    cas(
                        s,
                        proposal,
                        actor,
                        proposal.version,
                        status=status,
                        event="verification.workflow",
                        reason="trusted evidence pipeline",
                    )
                approval = ApprovalRequest(
                    proposal_id=proposal.id,
                    revision=proposal.version,
                    action="prototype.review.bundle",
                    evidence_hash=obj.sha256,
                    base_commit=bundle.base_commit,
                    target_branch=project.target_branch,
                    actor_id=proposal.author_agent_id,
                    expires_at=now() + timedelta(hours=1),
                )
                s.add(approval)
                s.flush()
            else:
                cas(
                    s,
                    proposal,
                    actor,
                    proposal.version,
                    status=State.FAILED,
                    event="verification.failed",
                    reason=bundle.verdict,
                )
            run.status, run.ended_at = bundle.verdict, now()
            run.evidence = {
                "mode": "prototype-verification.v1",
                "verified": False,
                "verification_id": j.verification_id,
                "bundle_artifact_id": obj.artifact_id,
                "bundle_sha256": obj.sha256,
                "gate": bundle.verdict,
            }
            if not self.preserve_budget:
                release(s, run)
            j = self._state(
                s,
                actor,
                row,
                current,
                state="COMPLETE",
                verdict=bundle.verdict,
                bundle_artifact_id=obj.artifact_id,
                bundle_sha256=obj.sha256,
                approval_id=approval.id if approval else None,
                proposal_version=proposal.version,
            )
        return self._info(j)

    def _review_inputs(self, actor, run_id, verification_id, checksum):
        require_human(actor)
        with self.factory() as s:
            run, proposal, project = self.workspaces._authorize(
                s, actor, run_id, None, cleanup=True
            )
            if run.lease_revoked_at is not None:
                deny("stale_or_revoked_review")
            row = s.get(IdempotencyRecord, verification_id)
            if not row or row.owner_id != actor.owner_id or row.key != self._key(run_id):
                deny("review_not_found", 404)
            j = VerificationJournal.model_validate(row.response)
            if (
                j.state != "COMPLETE"
                or j.verdict != "READY_FOR_HUMAN_REVIEW"
                or j.bundle_sha256 != checksum
            ):
                deny("review_bundle_not_acceptable")
        obj = self.store.read(actor, run_id, j.bundle_artifact_id)
        raw, proof = self.vault.open(self._capsule(j))
        if (
            obj.content != raw
            or hashlib.sha256(raw).hexdigest() != checksum
            or proof != {"issuer": "prototype-verification.v1"}
        ):
            deny("review_bundle_integrity_failed")
        bundle = ReviewBundle.model_validate_json(raw)
        with self.factory() as s:
            run, proposal, project = self.workspaces._authorize(
                s, actor, run_id, None, cleanup=True
            )
            if run.lease_revoked_at is not None:
                deny("stale_or_revoked_review")
            _, _, _, configuration, _ = self.workspaces._configuration(proposal, project)
            if (
                configuration != bundle.configuration_sha256
                or digest(proposal.document) != bundle.proposal_sha256
                or proposal.status == State.CANCELLED
            ):
                deny("review_authorization_revoked", 403)
        profile = approved_review_profile(self.qa.registry.get(bundle.profile_name))
        if profile.fingerprint != bundle.profile_sha256 or profile.image != bundle.image_id:
            deny("review_profile_changed", 403)
        for ref in bundle.artifacts:
            obj = self.store.read(actor, run_id, ref.artifact_id)
            if obj.evidence.artifact.sha256 != ref.sha256 or len(obj.content) != ref.size_bytes:
                deny("review_evidence_changed")
        with self.factory() as s:
            qa_row = s.get(IdempotencyRecord, bundle.qa_id)
            from hogwarts.qa.contracts import QAJournal

            if not qa_row:
                deny("review_qa_binding_changed")
            qa_j = QAJournal.model_validate(qa_row.response)
            if (
                qa_row.owner_id != actor.owner_id
                or qa_j.state != "READY"
                or qa_j.run_id != run_id
                or qa_j.build_id != bundle.build_id
                or qa_j.attestation_artifact_id != bundle.attestation_artifact_id
                or qa_j.profile_sha256 != bundle.profile_sha256
            ):
                deny("review_qa_binding_changed")
            _, observed = self.qa._result(qa_j)
            if (
                observed.outcome != "pass"
                or observed.exit_code != 0
                or not observed.cleanup_complete
            ):
                deny("review_qa_not_passing")
        return j, bundle

    def review(
        self,
        actor,
        run_id,
        verification_id,
        bundle_sha256,
        expected_version,
        decision,
        idempotency_key,
    ):
        try:
            return self._review(
                actor,
                run_id,
                verification_id,
                bundle_sha256,
                expected_version,
                decision,
                idempotency_key,
            )
        except (DomainError, SQLAlchemyError, OSError, ValueError) as error:
            code = error.code if isinstance(error, DomainError) else "review_infrastructure_error"
            try:
                with self.factory.begin() as s:
                    self._event(s, actor, run_id, "review_denied", code=code)
            except SQLAlchemyError:
                raise DomainError(503, "verification_database_and_audit_unavailable") from None
            raise DomainError(
                error.status if isinstance(error, DomainError) else 503, code
            ) from None

    def _review(
        self,
        actor,
        run_id,
        verification_id,
        bundle_sha256,
        expected_version,
        decision_name,
        idempotency_key,
    ):
        require_human(actor)
        data = HumanDecision(expected_version=expected_version, decision=decision_name)
        # Revalidate even on command replay: cached human acceptance is not new authority.
        j, bundle = self._review_inputs(actor, run_id, verification_id, bundle_sha256)
        body = {
            **data.model_dump(),
            "verification_id": verification_id,
            "bundle_sha256": bundle_sha256,
        }

        def decide(s):
            from hogwarts.leases.fencing import fence

            run = s.get(PrototypeRun, run_id)
            fence(s, run, active=False)
            proposal = proposal_for(s, run.proposal_id, actor)
            project = s.get(Project, proposal.project_id)
            _, _, _, configuration, _ = self.workspaces._configuration(proposal, project)
            ticket = s.get(ApprovalRequest, j.approval_id)
            if (
                proposal.version != expected_version
                or proposal.status != State.REVIEWING
                or run.status != "READY_FOR_HUMAN_REVIEW"
                or configuration != bundle.configuration_sha256
                or digest(proposal.document) != bundle.proposal_sha256
                or not ticket
                or ticket.consumed
                or ticket.decision != "PENDING"
                or utc(ticket.expires_at) <= now()
                or ticket.revision != expected_version
                or ticket.action != "prototype.review.bundle"
                or ticket.proposal_id != proposal.id
                or ticket.actor_id != proposal.author_agent_id
                or ticket.evidence_hash != bundle_sha256
                or ticket.base_commit != bundle.base_commit
                or ticket.target_branch != project.target_branch
            ):
                deny("stale_or_revoked_review")
            self._locked_evidence(s, actor, j, bundle)
            stored_bundle = self.store._get(s, actor, run_id, j.bundle_artifact_id, None)
            self.store._metadata(stored_bundle)
            if stored_bundle.sha256 != bundle_sha256:
                deny("review_bundle_integrity_failed")
            self.store.objects.read(stored_bundle.id, bundle_sha256, stored_bundle.size_bytes)
            cas(
                s,
                proposal,
                actor,
                expected_version,
                status=State.AWAITING_HUMAN_REVIEW,
                event="verification.human_review",
                reason="bundle-bound human decision",
            )
            result = decision(
                s,
                actor,
                proposal.id,
                HumanDecision(expected_version=proposal.version, decision=decision_name),
                idempotency_key,
            )
            ticket.consumed, ticket.decision, ticket.approver_id = True, decision_name, actor.id
            ticket.decided_at = now()
            if result["merge_request_id"]:
                # The intent references the human's exact bundle-bound approval.
                s.get(MergeRequest, result["merge_request_id"]).approval_id = ticket.id
            self._event(s, actor, run_id, "human_decided", j)
            return {
                **result,
                "verification_id": verification_id,
                "bundle_sha256": bundle_sha256,
                "review_approval_id": ticket.id,
            }

        return command(
            self.factory, actor, idempotency_key, "prototype.bundle.review", run_id, body, decide
        )

    def cleanup(self, actor, run_id, lease_token=None):
        with self.factory() as s:
            self.workspaces._authorize(s, actor, run_id, lease_token, cleanup=True)
            from hogwarts.qa.contracts import QAJournal

            jobs = [
                QAJournal.model_validate(r.response)
                for r in s.scalars(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.owner_id == actor.owner_id,
                        IdempotencyRecord.key.like("qa-runner.v1:%"),
                    )
                )
                if r.response.get("run_id") == run_id
            ]
        for job in jobs:
            self.qa.cleanup(actor, run_id, job.qa_id, lease_token)
        self.controller.destroy(actor, run_id, lease_token)
        self.workspaces.cleanup(actor, run_id, lease_token)
        with self.factory.begin() as s:
            run, proposal, _ = self.workspaces._authorize(
                s, actor, run_id, lease_token, cleanup=True
            )
            if run.status == "CLAIMED":
                row = s.scalar(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.owner_id == actor.owner_id,
                        IdempotencyRecord.key == self._key(run_id),
                    )
                )
                if row and row.response.get("state") in {"BLOCKED", "UNKNOWN"}:
                    run.status, run.ended_at = row.response["state"], now()
                    cas(
                        s,
                        proposal,
                        actor,
                        proposal.version,
                        status=State.FAILED,
                        event="verification.cleanup_terminal",
                        reason=run.status,
                    )
                    if not self.preserve_budget:
                        release(s, run)
