"""Recovery effects are whitelisted publication or proven terminal bookkeeping only."""

import json

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from hogwarts.models import IdempotencyRecord, OutputImport, PrototypeRun, now
from hogwarts.qa.contracts import QAJournal
from hogwarts.recovery.accounting import accounting, settle
from hogwarts.recovery.contracts import Decision, Observation, RecoveryJournal, deny
from hogwarts.service import DomainError, digest
from hogwarts.verification.contracts import VerificationInfo, VerificationJournal


class RecoveryExecutor:
    def __init__(self, planner):
        self.p = planner

    @staticmethod
    def fence(s, run_id, observation):
        with s.no_autoflush:
            changed = s.execute(
                update(PrototypeRun)
                .where(
                    PrototypeRun.id == run_id,
                    PrototypeRun.lease_generation == observation.generation,
                    PrototypeRun.lease_version == observation.lease_version,
                )
                .values(lease_version=PrototypeRun.lease_version)
                .execution_options(synchronize_session=False)
            )
        if changed.rowcount != 1:
            deny("recovery_generation_fence_failed")

    def execute(self, actor, run_id, identifier, version, token=None):
        if type(version) is not int or version < 1:
            deny("recovery_invalid_version", 403)
        p = self.p
        with p.factory.begin() as s:
            p.inspector.context(s, actor, run_id, token)
            row, j = p.row(s, actor, run_id, identifier)
            if j.state == "COMPLETE" and version in {1, j.version}:
                return j
            if j.version != version:
                deny("recovery_cas_conflict")
            if j.state in {"UNKNOWN", "MANUAL_REQUIRED"}:
                return j
            if j.state == "EXECUTING":
                return p.state(
                    s, actor, row, j, "UNKNOWN", "recovery_coordinator_outcome_uncertain"
                )
            current, _ = p.inspector.inspect(s, actor, run_id, token)
            if (
                current.generation != j.observation.generation
                or current.lease_version != j.observation.lease_version
                or current.fingerprint != j.observation.fingerprint
                or current.decision != j.observation.decision
            ):
                return p.state(s, actor, row, j, "MANUAL_REQUIRED", "recovery_observation_changed")
            if current.decision.classification == "MANUAL_REQUIRED":
                return p.state(s, actor, row, j, "MANUAL_REQUIRED", current.decision.reason)
            if current.decision.classification == "SAFE_RESUME":
                for other in s.scalars(
                    select(IdempotencyRecord).where(
                        IdempotencyRecord.owner_id == actor.owner_id,
                        IdempotencyRecord.key.like("workflow-recovery.v1:%"),
                    )
                ):
                    oj = RecoveryJournal.model_validate_json(json.dumps(other.response))
                    if (
                        other.id != j.recovery_id
                        and oj.observation.run_id == run_id
                        and oj.state in {"EXECUTING", "UNKNOWN"}
                    ):
                        return p.state(
                            s,
                            actor,
                            row,
                            j,
                            "MANUAL_REQUIRED",
                            "recovery_other_coordinator_uncertain",
                        )
            self.fence(s, run_id, j.observation)
            j = p.state(s, actor, row, j, "EXECUTING")
        try:
            published = None
            if j.observation.decision.classification == "SAFE_RESUME":
                published = self.publish(actor, run_id, j, token)
            with p.factory.begin() as s:
                run, proposal, _, _ = p.inspector.context(s, actor, run_id, token)
                row, current = p.row(s, actor, run_id, identifier)
                if current.state != "EXECUTING" or current.version != j.version:
                    deny("recovery_execution_uncertain")
                if (
                    run.lease_generation != j.observation.generation
                    or run.lease_version != j.observation.lease_version
                ):
                    deny("recovery_generation_changed")
                self.fence(s, run_id, j.observation)
                action = j.observation.decision.action
                committed_verification = action == "VERIFICATION" and published is not None
                if committed_verification:
                    obs = self.committed_result(s, actor, run, proposal, j, published)
                else:
                    obs, _ = p.inspector.inspect(s, actor, run_id, token, lock_budget=True)
                if j.observation.decision.classification == "FINALIZE":
                    if (
                        obs.fingerprint != j.observation.fingerprint
                        or obs.decision != j.observation.decision
                    ):
                        deny("recovery_finalization_changed")
                    if action == "SETTLE_BUDGET":
                        settle(s, run, proposal, actor, obs.proof_sha256)
                    elif action == "CANCEL_METADATA":
                        changed = s.execute(
                            update(PrototypeRun)
                            .where(
                                PrototypeRun.id == run_id,
                                PrototypeRun.lease_generation == obs.generation,
                                PrototypeRun.lease_version == obs.lease_version,
                                PrototypeRun.status == run.status,
                            )
                            .values(status="CANCELLED", ended_at=now())
                        )
                        if changed.rowcount != 1:
                            deny("recovery_run_cas_conflict")
                elif obs.decision.classification == "MANUAL_REQUIRED" and obs.decision.reason in {
                    "recovery_durable_proof_invalid",
                    "recovery_artifact_integrity_failed",
                    "recovery_resource_identity_uncertain",
                    "recovery_resources_unavailable",
                    "recovery_budget_counters_inconsistent",
                }:
                    deny(obs.decision.reason)
                if committed_verification:
                    after = obs
                else:
                    after, _ = p.inspector.inspect(s, actor, run_id, token)
                if any(
                    after.artifact_hashes.get(k) != h
                    for k, h in j.observation.artifact_hashes.items()
                ):
                    deny("recovery_historical_evidence_changed")
                done = p.state(s, actor, row, current, "COMPLETE", result=after)
                if after.decision.classification == "MANUAL_REQUIRED":
                    p.interventions.put(s, actor, done, after.decision.reason, after)
                return done
        except (DomainError, SQLAlchemyError, OSError, ValueError, KeyError, TypeError) as error:
            code = error.code if isinstance(error, DomainError) else "recovery_operation_uncertain"
            with p.factory.begin() as s:
                p.inspector.context(s, actor, run_id, token)
                row, current = p.row(s, actor, run_id, identifier)
                if current.state == "COMPLETE":
                    return current
                return p.state(s, actor, row, current, "UNKNOWN", code)

    def committed_result(self, s, actor, run, proposal, journal, result):
        """Receipt of the trusted publisher commit, not a historical Store read.

        The publisher validated bytes under its live M1 fence. A completed worker
        may acknowledge that returned result without gaining fresh read authority.
        """
        if (
            not isinstance(result, VerificationInfo)
            or result.verification_id != journal.observation.decision.target_id
            or result.run_id != run.id
            or run.worker_id != actor.id
            or result.status != run.status
        ):
            deny("recovery_publisher_receipt_mismatch")
        row = s.get(IdempotencyRecord, result.verification_id)
        if not row or row.owner_id != actor.owner_id or row.actor_id != actor.id:
            deny("recovery_publisher_receipt_mismatch")
        state = VerificationJournal.model_validate(row.response)
        evidence = run.evidence or {}
        if (
            state.state != "COMPLETE"
            or state.bundle_artifact_id != result.bundle_artifact_id
            or state.bundle_sha256 != result.bundle_sha256
            or evidence.get("verification_id") != result.verification_id
            or evidence.get("bundle_artifact_id") != result.bundle_artifact_id
            or evidence.get("bundle_sha256") != result.bundle_sha256
            or evidence.get("verified") is not False
        ):
            deny("recovery_publisher_receipt_mismatch")
        budget = accounting(s, run, proposal, actor.owner_id, lock=True)
        if not budget["valid"] or budget["status"] != "RELEASED" or budget["actual"] != 0:
            deny("recovery_publisher_accounting_mismatch")
        data = {
            "trusted_publisher_result": result.model_dump(mode="json"),
            "run_status": run.status,
            "proposal_status": proposal.status,
            "budget": budget,
            "generation": run.lease_generation,
        }
        hashes = dict(journal.observation.artifact_hashes)
        hashes[result.bundle_artifact_id] = result.bundle_sha256
        return Observation(
            run_id=run.id,
            project_id=journal.observation.project_id,
            generation=run.lease_generation,
            lease_version=run.lease_version,
            fingerprint=digest(data),
            stalled=False,
            decision=Decision(
                classification="FINALIZE", reason="recovery_trusted_publisher_committed"
            ),
            artifact_hashes=hashes,
            component_hashes={"trusted_publisher_commit": digest(data)},
        )

    def publish(self, actor, run_id, j, token):
        p = self.p
        with p.factory.begin() as s:
            run, _, _, _ = p.inspector.context(s, actor, run_id, token)
            obs, _ = p.inspector.inspect(s, actor, run_id, token)
            if (
                obs.fingerprint != j.observation.fingerprint
                or obs.decision != j.observation.decision
                or run.lease_generation != j.observation.generation
                or run.lease_version != j.observation.lease_version
            ):
                deny("recovery_publication_changed")
            self.p.inspector.workspaces._authorize(s, actor, run_id, token)
            target = j.observation.decision.target_id
            action = j.observation.decision.action
            record = s.get(OutputImport if action == "IMPORT" else IdempotencyRecord, target)
            if record is None:
                deny("recovery_target_missing")
            data = record.response if action != "IMPORT" else None
        o = p.inspector.o
        if action == "IMPORT":
            return o.importer.resume_publication(
                actor, run_id, record.workspace_id, record.sandbox_id, token
            )
        elif action == "PATCH":
            return o.builder.resume_publication(actor, run_id, data["import_id"], token)
        elif action == "QA":
            q = QAJournal.model_validate(data)
            return o.qa.resume_publication(actor, run_id, q.build_id, q.profile_name, token)
        elif action == "VERIFICATION":
            v = VerificationJournal.model_validate(data)
            return o.resume_publication(actor, run_id, token, v.runtime_profile, v.qa_profile)
        else:
            deny("recovery_action_not_permitted", 403)
