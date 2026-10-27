"""At-most-once execution intent, authenticated result, publication-only recovery."""

import hashlib
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from hogwarts.artifacts.contracts import ArtifactUpload
from hogwarts.models import IdempotencyRecord, WorkflowEvent, now
from hogwarts.outputs.capsule import CapsuleVault, canonical
from hogwarts.qa.contracts import (
    VERSION,
    ExecutionResult,
    QAError,
    QAExecutionAttestation,
    QAInfo,
    QAJournal,
    deny,
)
from hogwarts.qa.inputs import QAInputValidator
from hogwarts.service import DomainError, digest, utc
from hogwarts.workspaces.manager import token_hash


class LocalTrustedQARunner:
    def __init__(self, builder, root, driver, registry):
        self.builder, self.factory, self.store = builder, builder.factory, builder.store
        self.workspaces, self.driver, self.registry = builder.workspaces, driver, registry
        self.validator = QAInputValidator(builder, driver.controller_id)
        self.vault = CapsuleVault(
            root,
            (
                builder.objects.root,
                self.store.objects.root,
                builder.importer.vault.objects.root,
                self.workspaces.root,
                *(g.repository for g in self.workspaces.grants.values()),
            ),
        )

    def _event(self, s, actor, run, event, state=None, code=None):
        s.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run,
                aggregate_version=state.version if state else 0,
                event_type="qa." + event,
                source="trusted_qa_runner",
                actor_id=actor.id,
                reason=event,
                payload={"qa_id": state.qa_id if state else None, "code": code},
            )
        )

    def _row(self, s, actor, key):
        row = s.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == actor.owner_id,
                IdempotencyRecord.actor_id == actor.id,
                IdempotencyRecord.key == key,
            )
        )
        if row:
            s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id)
                .values(request_hash=IdempotencyRecord.request_hash)
            )
            s.refresh(row)
        return row

    def _journal(self, row, inputs, profile, actor, token):
        if not row or row.request_hash != inputs.fingerprint:
            deny("qa_request_conflict")
        j = QAJournal.model_validate(row.response)
        c = inputs.context
        if (
            j.qa_id != row.id
            or j.run_id != c.run_id
            or j.build_id != inputs.build.build_id
            or j.workspace_id != c.workspace_id
            or j.sandbox_id != c.sandbox_id
            or j.import_id != c.import_id
            or j.controller_id != self.driver.controller_id
            or j.worker_id != actor.id
            or j.lease_hash != token_hash(token)
            or j.profile_name != profile.name
            or j.profile_sha256 != profile.fingerprint
            or j.request_hash != inputs.fingerprint
            or j.input_sha256 != hashlib.sha256(inputs.archive).hexdigest()
        ):
            deny("qa_journal_binding_mismatch")
        return j

    def _state(self, s, actor, row, j, state, **changes):
        result = QAJournal.model_validate(
            {**j.model_dump(mode="json"), **changes, "state": state, "version": j.version + 1}
        )
        row.response = result.model_dump(mode="json")
        self._event(s, actor, j.run_id, state.lower(), result)
        return result

    def _capsule_row(self, state):
        j = QAJournal.model_validate(state) if isinstance(state, dict) else state
        return SimpleNamespace(
            id=j.qa_id,
            run_id=j.run_id,
            workspace_id=j.workspace_id,
            sandbox_id=j.qa_id,
            request_hash=j.request_hash,
            seal_key=j.seal_key,
        )

    def _resource(self, j):
        return SimpleNamespace(
            id=j.qa_id,
            run_id=j.run_id,
            workspace_id=j.workspace_id,
            request_hash=j.profile_sha256,
            container_id=j.container_id,
            expires_at=datetime.fromisoformat(j.expires_at),
        )

    def _result(self, j):
        raw, proof = self.vault.open(self._capsule_row(j))
        if proof != {"issuer": VERSION, "input_sha256": j.input_sha256}:
            deny("qa_result_binding_mismatch")
        result = ExecutionResult.model_validate_json(raw)
        if j.result_sha256 and hashlib.sha256(raw).hexdigest() != j.result_sha256:
            deny("qa_result_integrity_failed")
        return raw, result

    def run(self, actor, run_id, build_id, profile_name, lease_token):
        try:
            return self._run(actor, run_id, build_id, profile_name, lease_token)
        except (DomainError, SQLAlchemyError, OSError, ValueError, TypeError) as error:
            code = (
                error.code
                if isinstance(error, DomainError)
                else "qa_database_failed"
                if isinstance(error, SQLAlchemyError)
                else "qa_schema_invalid"
                if isinstance(error, (ValidationError, ValueError, TypeError))
                else "qa_storage_failed"
            )
            try:
                with self.factory.begin() as s:
                    self._event(s, actor, run_id, "denied", code=code)
            except SQLAlchemyError:
                raise QAError(503, "qa_database_and_audit_unavailable") from None
            raise QAError(error.status if isinstance(error, DomainError) else 503, code) from None

    def resume_publication(self, actor, run_id, build_id, profile_name, token):
        """Only publish a complete sealed result; workload execution is forbidden."""
        return self._run(actor, run_id, build_id, profile_name, token, publication_only=True)

    def _run(self, actor, run_id, build_id, profile_name, token, *, publication_only=False):
        profile = self.registry.get(profile_name)
        inputs = self.validator.load(actor, run_id, build_id, token, profile)
        c = inputs.context
        key = "qa-runner.v1:" + digest({"build": build_id, "profile": profile.name})
        try:
            with self.factory.begin() as s:
                run, proposal, _ = self.workspaces._authorize(s, actor, run_id, token)
                row = self._row(s, actor, key)
                if publication_only and (
                    row is None
                    or row.response.get("state")
                    not in {"EXECUTING", "CAPTURED", "PUBLISHING", "READY"}
                ):
                    deny("qa_publication_proof_required")
                if not row:
                    seconds = min(
                        profile.lifetime_seconds,
                        proposal.document["prototype"]["max_runtime_seconds"],
                        int((utc(run.lease_expires_at) - now()).total_seconds()),
                    )
                    if seconds < 2:
                        deny("qa_lease_too_short", 403)
                    j = QAJournal(
                        qa_id=str(uuid4()),
                        run_id=run_id,
                        build_id=build_id,
                        import_id=c.import_id,
                        workspace_id=c.workspace_id,
                        sandbox_id=c.sandbox_id,
                        worker_id=actor.id,
                        controller_id=self.driver.controller_id,
                        lease_hash=token_hash(token),
                        request_hash=inputs.fingerprint,
                        profile_name=profile.name,
                        profile_sha256=profile.fingerprint,
                        seal_key=secrets.token_hex(32),
                        expires_at=(now() + timedelta(seconds=seconds)).isoformat(),
                        input_sha256=hashlib.sha256(inputs.archive).hexdigest(),
                    )
                    s.add(
                        IdempotencyRecord(
                            id=j.qa_id,
                            owner_id=actor.owner_id,
                            actor_id=actor.id,
                            key=key,
                            request_hash=inputs.fingerprint,
                            response=j.model_dump(mode="json"),
                        )
                    )
                    self._event(s, actor, run_id, "pending", j)
        except IntegrityError:
            pass  # Uniqueness serializes independent services; validate the winner below.
        execute = False
        with self.factory.begin() as s:
            self.builder._context(s, actor, run_id, c.import_id, token, c)
            row = self._row(s, actor, key)
            j = self._journal(row, inputs, profile, actor, token)
            if publication_only and (
                j.state == "PENDING" or not self.vault.exists(self._capsule_row(j))
            ):
                deny("qa_publication_proof_required")
            if j.state == "PENDING":
                if j.cleanup_requested:
                    deny("qa_cleanup_tombstone")
                j = self._state(s, actor, row, j, "EXECUTING")
                execute = True
            elif j.state == "EXECUTING" and not self.vault.exists(self._capsule_row(j)):
                deny("qa_execution_busy_or_unknown")
        if execute:
            if publication_only:
                deny("qa_recovery_execution_forbidden")

            def authorize_execution():
                with self.factory() as session:
                    self.builder._context(session, actor, run_id, c.import_id, token, c)
                    record = session.get(IdempotencyRecord, j.qa_id)
                    current = self._journal(record, inputs, profile, actor, token)
                    if current.cleanup_requested:
                        deny("qa_cleanup_tombstone")

            result = self._execute(j, profile, inputs.archive, authorize_execution)
            self.vault.seal(
                self._capsule_row(j),
                canonical(result.model_dump(mode="json")),
                {"issuer": VERSION, "input_sha256": j.input_sha256},
            )
        raw, result = self._result(j)
        if result.image_id != profile.image:
            deny("qa_result_image_mismatch")
        with self.factory.begin() as s:
            self.builder._context(s, actor, run_id, c.import_id, token, c)
            row = self._row(s, actor, key)
            j = self._journal(row, inputs, profile, actor, token)
            if j.state == "EXECUTING":
                j = self._state(
                    s,
                    actor,
                    row,
                    j,
                    "CAPTURED",
                    result_sha256=hashlib.sha256(raw).hexdigest(),
                    outcome=result.outcome,
                    cleanup_complete=result.cleanup_complete,
                )
        # Revalidation on every publication retry; execution NEVER occurs here.
        fresh = self.validator.load(actor, run_id, build_id, token, profile)
        if fresh.fingerprint != inputs.fingerprint:
            deny("qa_inputs_changed")
        return self._publish(actor, run_id, token, profile, inputs, key, j, result)

    def _execute(self, j, profile, archive, authorize):
        row = self._resource(j)
        start = now().isoformat()
        try:
            if utc(row.expires_at) <= now():
                deny("qa_deadline_expired")
            authorize()
            row.container_id = self.driver.create(row, profile)
            authorize()
            self.driver.start(row, profile, archive)
            authorize()
            seconds = min(
                profile.timeout_seconds, int((utc(row.expires_at) - now()).total_seconds())
            )
            if seconds < 1:
                deny("qa_deadline_expired")
            result = ExecutionResult.model_validate(self.driver.execute(row, profile, seconds))
        except (DomainError, OSError, ValueError, TypeError) as error:
            result = ExecutionResult(
                outcome="unknown_execution",
                exit_code=None,
                stdout="",
                stderr="",
                started_at=start,
                finished_at=now().isoformat(),
                container_id=None,
                image_id=profile.image,
                error_code=error.code
                if isinstance(error, DomainError)
                else "qa_execution_uncertain",
            )
        try:
            self.driver.destroy(row)
            result = result.model_copy(update={"cleanup_complete": True})
        except (DomainError, OSError, ValueError):
            result = result.model_copy(
                update={
                    "cleanup_complete": False,
                    "outcome": "unknown_execution"
                    if result.outcome == "unknown_execution"
                    else "infrastructure_error",
                    "error_code": "qa_cleanup_failed",
                }
            )
        return result

    def _publish(self, actor, run_id, token, profile, inputs, key, j, result):
        c, build = inputs.context, inputs.build
        with self.factory.begin() as s:
            self.builder._context(s, actor, run_id, c.import_id, token, c)
            row = self._row(s, actor, key)
            j = self._journal(row, inputs, profile, actor, token)
            if j.state not in {"CAPTURED", "PUBLISHING", "READY"}:
                deny("qa_not_publishable")
            if j.state == "CAPTURED":
                j = self._state(s, actor, row, j, "PUBLISHING")

        def put(suffix, data, kind, media):
            return self.store.put(
                actor,
                run_id,
                c.workspace_id,
                c.sandbox_id,
                token,
                "qa_" + j.qa_id.replace("-", "") + "_" + suffix,
                ArtifactUpload(kind=kind, media_type=media),
                data,
            ).artifact

        def log(suffix, data):
            obj = put(suffix, data, "log", "application/octet-stream")
            return {
                "artifact_id": obj.artifact_id,
                "sha256": obj.sha256,
                "size_bytes": obj.size_bytes,
            }

        stdout, stderr = (
            log("stdout", bytes.fromhex(result.stdout)),
            log("stderr", bytes.fromhex(result.stderr)),
        )
        from hogwarts.qa.driver import container_argv

        attestation = QAExecutionAttestation(
            qa_id=j.qa_id,
            run_id=run_id,
            worker_id=actor.id,
            controller_id=self.driver.controller_id,
            workspace_id=c.workspace_id,
            original_sandbox_id=c.sandbox_id,
            qa_sandbox_id=j.qa_id,
            build_id=build.build_id,
            patch_artifact_id=build.patch_artifact_id,
            patch_sha256=build.patch_sha256,
            build_proof_artifact_id=build.proof_artifact_id,
            build_proof_sha256=build.proof_sha256,
            base_commit=c.base_commit,
            result_tree=build.proof.result_tree,
            profile_name=profile.name,
            profile_sha256=profile.fingerprint,
            argv=list(profile.argv),
            container_argv=container_argv(self._resource(j), profile),
            image_id=result.image_id,
            input_sha256=j.input_sha256,
            resource_limits={
                "cpus": profile.cpus,
                "memory_mb": profile.memory_mb,
                "pids": profile.pids,
                "timeout_seconds": profile.timeout_seconds,
                "absolute_expires_at": j.expires_at,
                "uid": 10001,
                "gid": 10001,
                "network": "none",
                "rootfs": "readonly",
                "capabilities": [],
                "no_new_privileges": True,
                "input": "readonly managed tmpfs volume",
                "output_bytes": 16777216,
                "tmp_bytes": 8388608,
            },
            container_id=result.container_id,
            outcome=result.outcome,
            exit_code=result.exit_code,
            started_at=result.started_at,
            finished_at=result.finished_at,
            oom_killed=result.oom_killed,
            timed_out=result.timed_out,
            cleanup_complete=result.cleanup_complete,
            error_code=result.error_code,
            stdout=stdout,
            stderr=stderr,
        )
        obj = put(
            "attestation",
            canonical(attestation.model_dump(mode="json")),
            "report",
            "application/json",
        )
        with self.factory.begin() as s:
            self.builder._context(s, actor, run_id, c.import_id, token, c)
            row = self._row(s, actor, key)
            j = self._journal(row, inputs, profile, actor, token)
            if j.state == "READY":
                if (
                    j.attestation_artifact_id != obj.artifact_id
                    or j.attestation_sha256 != obj.sha256
                ):
                    deny("qa_attestation_changed")
            elif j.state == "PUBLISHING":
                j = self._state(
                    s,
                    actor,
                    row,
                    j,
                    "READY",
                    attestation_artifact_id=obj.artifact_id,
                    attestation_sha256=obj.sha256,
                )
            else:
                deny("qa_finalize_conflict")
        return QAInfo(
            qa_id=j.qa_id,
            run_id=run_id,
            outcome=result.outcome,
            attestation_artifact_id=obj.artifact_id,
            attestation_sha256=obj.sha256,
        )

    def cleanup(self, actor, run_id, qa_id, lease_token=None):
        try:
            return self._cleanup(actor, run_id, qa_id, lease_token)
        except (DomainError, SQLAlchemyError, OSError, ValueError) as error:
            code = error.code if isinstance(error, DomainError) else "qa_cleanup_failed"
            try:
                with self.factory.begin() as session:
                    self._event(session, actor, run_id, "cleanup_failed", code=code)
            except SQLAlchemyError:
                raise QAError(503, "qa_database_and_audit_unavailable") from None
            raise QAError(409, code) from None

    def _cleanup(self, actor, run_id, qa_id, lease_token=None):
        with self.factory.begin() as s:
            self.workspaces._authorize(s, actor, run_id, lease_token, cleanup=True)
            row = s.get(IdempotencyRecord, qa_id)
            if not row or row.owner_id != actor.owner_id or not row.key.startswith("qa-runner.v1:"):
                deny("qa_cleanup_not_authorized", 403)
            s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id)
                .values(request_hash=IdempotencyRecord.request_hash)
            )
            s.refresh(row)
            j = QAJournal.model_validate(row.response)
            if (
                j.controller_id != self.driver.controller_id
                or j.qa_id != row.id
                or j.run_id != run_id
                or (
                    actor.role != "human"
                    and (j.worker_id != actor.id or j.lease_hash != token_hash(lease_token))
                )
            ):
                deny("qa_cleanup_binding_mismatch", 403)
            # Commit revocation BEFORE daemon cleanup, so a racing creator cannot
            # treat a successful deletion as permission for a fresh execution.
            j = self._state(s, actor, row, j, j.state, cleanup_requested=True)
            self._event(s, actor, run_id, "cleanup_requested", j)
        self.driver.destroy(self._resource(j))
        with self.factory.begin() as s:
            row = s.get(IdempotencyRecord, qa_id)
            s.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id)
                .values(request_hash=IdempotencyRecord.request_hash)
            )
            s.refresh(row)
            current = QAJournal.model_validate(row.response)
            result = current.model_copy(
                update={"cleanup_complete": True, "version": current.version + 1}
            )
            row.response = result.model_dump(mode="json")
            self._event(s, actor, run_id, "cleaned", result)
