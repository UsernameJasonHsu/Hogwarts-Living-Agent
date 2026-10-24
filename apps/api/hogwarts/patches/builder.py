"""Durable trusted pure patch construction; no merge, code execution or QA promotion."""

import hashlib

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from hogwarts.artifacts.contracts import ArtifactUpload
from hogwarts.artifacts.filesystem import LocalObjects
from hogwarts.models import IdempotencyRecord, WorkflowEvent
from hogwarts.outputs.capsule import canonical
from hogwarts.patches.contracts import VERSION, BuildJournal, PatchError, PatchInfo, deny
from hogwarts.patches.git import PrivateGit
from hogwarts.patches.inputs import InputValidator
from hogwarts.service import DomainError, digest


class LocalGitPatchBuilder:
    def __init__(self, importer, root):
        self.importer, self.store, self.workspaces = importer, importer.store, importer.workspaces
        self.factory = importer.factory
        self.objects = LocalObjects(
            root,
            (
                self.store.objects.root,
                importer.vault.objects.root,
                self.workspaces.root,
                *(g.repository for g in self.workspaces.grants.values()),
            ),
        )
        self.git = PrivateGit(self.objects.root, self.workspaces.git)
        self.validator = InputValidator(importer, self.git.version)

    def _key(self, import_id):
        return "patch-builder.v1:" + import_id

    def _row(self, session, actor, import_id):
        row = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == actor.owner_id,
                IdempotencyRecord.actor_id == actor.id,
                IdempotencyRecord.key == self._key(import_id),
            )
        )
        if row:
            session.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.id == row.id)
                .values(request_hash=IdempotencyRecord.request_hash)
            )
            session.refresh(row)
        return row

    def _journal(self, row, c):
        if not row or row.request_hash != c.fingerprint:
            deny("patch_request_conflict")
        state = BuildJournal.model_validate(row.response)
        if (
            state.build_id != row.id
            or state.run_id != c.run_id
            or state.import_id != c.import_id
            or state.workspace_id != c.workspace_id
            or state.sandbox_id != c.sandbox_id
        ):
            deny("patch_journal_binding_mismatch")
        return state

    def _event(self, session, actor, run_id, event, journal=None, **payload):
        session.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=journal.version if journal else 0,
                event_type="patch_build." + event,
                source="trusted_git_patch_builder",
                actor_id=actor.id,
                reason=event,
                payload={
                    **payload,
                    **(
                        {"build_id": journal.build_id, "import_id": journal.import_id}
                        if journal
                        else {}
                    ),
                },
            )
        )

    def _state(self, session, actor, row, journal, state, **changes):
        result = BuildJournal.model_validate(
            {
                **journal.model_dump(mode="json"),
                **changes,
                "state": state,
                "version": journal.version + 1,
            }
        )
        row.response = result.model_dump(mode="json")
        self._event(session, actor, journal.run_id, state.lower(), result, code=result.error_code)
        return result

    def _context(self, session, actor, run_id, import_id, token, expected):
        session.expire_all()
        actual = self.validator.context(session, actor, run_id, import_id, token)
        if actual.fingerprint != expected.fingerprint:
            deny("patch_inputs_changed")
        return actual

    def _candidate(self, journal, inputs):
        proof = journal.proof
        if (
            not proof
            or not journal.patch_sha256
            or not journal.proof_sha256
            or digest(proof.model_dump(mode="json")) != journal.proof_sha256
            or proof.builder_version != VERSION
            or proof.base_commit != inputs.context.base_commit
            or proof.import_manifest_sha256 != inputs.context.manifest_sha256
            or proof.git_version != self.git.version
            or proof.source_files != inputs.manifest.files
        ):
            deny("patch_candidate_proof_invalid")
        path = self.objects.path(journal.build_id)
        from hogwarts.workspaces.git import plain

        size = plain(path).st_size
        return self.objects.read(journal.build_id, journal.patch_sha256, size), proof

    def build(self, actor, run_id, import_id, lease_token):
        try:
            return self._build(actor, run_id, import_id, lease_token)
        except (DomainError, SQLAlchemyError, OSError, ValueError, TypeError) as failure:
            code = (
                failure.code
                if isinstance(failure, DomainError)
                else "patch_database_failed"
                if isinstance(failure, SQLAlchemyError)
                else "patch_schema_invalid"
                if isinstance(failure, ValidationError)
                else "patch_storage_failed"
            )
            try:
                with self.factory.begin() as session:
                    self._event(session, actor, run_id, "denied", code=code)
            except SQLAlchemyError:
                raise PatchError(503, "patch_database_and_audit_unavailable") from None
            raise PatchError(
                503
                if isinstance(failure, (SQLAlchemyError, OSError))
                else getattr(failure, "status", 409),
                code,
            ) from None

    def _build(self, actor, run_id, import_id, token):
        inputs = self.validator.load(actor, run_id, import_id, token)
        c = inputs.context
        try:
            with self.factory.begin() as session:
                self._context(session, actor, run_id, import_id, token, c)
                row = self._row(session, actor, import_id)
                if row is None:
                    row = IdempotencyRecord(
                        owner_id=actor.owner_id,
                        actor_id=actor.id,
                        key=self._key(import_id),
                        request_hash=c.fingerprint,
                        response={},
                    )
                    session.add(row)
                    session.flush()
                    state = BuildJournal(
                        build_id=row.id,
                        run_id=run_id,
                        import_id=import_id,
                        workspace_id=c.workspace_id,
                        sandbox_id=c.sandbox_id,
                    )
                    row.response = state.model_dump(mode="json")
                    self._event(session, actor, run_id, "pending", state)
                else:
                    self._journal(row, c)
        except IntegrityError:
            with self.factory() as session:
                if not session.scalar(
                    select(IdempotencyRecord.id).where(
                        IdempotencyRecord.owner_id == actor.owner_id,
                        IdempotencyRecord.actor_id == actor.id,
                        IdempotencyRecord.key == self._key(import_id),
                    )
                ):
                    raise
        with self.factory.begin() as session:
            self._context(session, actor, run_id, import_id, token, c)
            row = self._row(session, actor, import_id)
            state = self._journal(row, c)
            if state.state == "PENDING":
                state = self._state(session, actor, row, state, "BUILDING")
        error = None
        with self.factory.begin() as session:
            row = self._row(session, actor, import_id)
            state = self._journal(row, c)
            self._context(session, actor, run_id, import_id, token, c)
            if state.state == "FAILED":
                deny("patch_build_failed")
            try:
                if state.state == "BUILDING":
                    patch, proof = self.git.construct(inputs)
                    checksum = hashlib.sha256(patch).hexdigest()
                    self.objects.publish(state.build_id, patch, checksum)
                    self._context(session, actor, run_id, import_id, token, c)
                    state = self._state(
                        session,
                        actor,
                        row,
                        state,
                        "BUILT",
                        patch_sha256=checksum,
                        proof=proof.model_dump(mode="json"),
                        proof_sha256=digest(proof.model_dump(mode="json")),
                    )
                else:
                    patch, proof = self._candidate(state, inputs)
            except (DomainError, OSError, ValueError) as failure:
                error = failure
                state = self._state(
                    session,
                    actor,
                    row,
                    state,
                    "FAILED",
                    error_code=failure.code
                    if isinstance(failure, DomainError)
                    else "patch_construct_failed",
                )
        if error:
            raise error
        # Re-read immutable source artifacts after computation; never hold a second DB writer.
        refreshed = self.validator.load(actor, run_id, import_id, token)
        if refreshed.context.fingerprint != c.fingerprint or refreshed.contents != inputs.contents:
            deny("patch_inputs_changed")
        with self.factory.begin() as session:
            self._context(session, actor, run_id, import_id, token, c)
            row = self._row(session, actor, import_id)
            state = self._journal(row, c)
            if state.state not in {"BUILT", "PUBLISHING", "READY"}:
                deny("patch_not_publishable")
            if state.state == "BUILT":
                state = self._state(session, actor, row, state, "PUBLISHING")

        def put(suffix, content, kind, media):
            with self.factory() as session:
                self._context(session, actor, run_id, import_id, token, c)
            return self.store.put(
                actor,
                run_id,
                c.workspace_id,
                c.sandbox_id,
                token,
                "pb_" + state.build_id.replace("-", "") + "_" + suffix,
                ArtifactUpload(kind=kind, media_type=media),
                content,
            ).artifact

        patch_artifact = put("patch", patch, "patch", "application/octet-stream")
        proof_artifact = put(
            "proof", canonical(proof.model_dump(mode="json")), "report", "application/json"
        )
        # A mid-publication source/authorization change must not finalize a completed bundle.
        final_inputs = self.validator.load(actor, run_id, import_id, token)
        if final_inputs.context.fingerprint != c.fingerprint:
            deny("patch_inputs_changed")
        with self.factory.begin() as session:
            self._context(session, actor, run_id, import_id, token, c)
            row = self._row(session, actor, import_id)
            state = self._journal(row, c)
            if state.state not in {"PUBLISHING", "READY"}:
                deny("patch_finalize_conflict")
            if state.patch_artifact_id and (
                state.patch_artifact_id != patch_artifact.artifact_id
                or state.proof_artifact_id != proof_artifact.artifact_id
            ):
                deny("patch_published_artifact_changed")
            if patch_artifact.sha256 != state.patch_sha256:
                deny("patch_published_hash_mismatch")
            state = self._state(
                session,
                actor,
                row,
                state,
                "READY",
                patch_artifact_id=patch_artifact.artifact_id,
                proof_artifact_id=proof_artifact.artifact_id,
            )
            return PatchInfo(
                build_id=state.build_id,
                run_id=run_id,
                import_id=import_id,
                patch_artifact_id=state.patch_artifact_id,
                proof_artifact_id=state.proof_artifact_id,
                patch_sha256=state.patch_sha256,
                changed_paths=proof.changed_paths,
                applicability=proof.applicability,
            )
