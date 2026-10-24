"""DB-authorized immutable local storage. No content-based trust or QA promotion."""

import hashlib
import json
import re

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError as DatabaseIntegrityError
from sqlalchemy.exc import SQLAlchemyError

from hogwarts.artifacts.contracts import (
    MAX_BYTES,
    ArtifactError,
    ArtifactRead,
    ArtifactUpload,
    Assessment,
    EvidenceDocument,
    ExecutionSnapshot,
    ImmutableArtifact,
    IntegrityError,
    reject,
)
from hogwarts.artifacts.filesystem import LocalObjects
from hogwarts.models import ArtifactMetadata, GitWorkspace, SandboxInstance, WorkflowEvent, now
from hogwarts.service import DomainError, digest, utc
from hogwarts.workspaces.manager import token_hash


def canonical(metadata):
    return json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def metadata_hash(row):
    return hashlib.sha256(
        canonical({"schema_version": row.schema_version, "artifact": row.evidence}).encode()
    ).hexdigest()


class LocalArtifactStore:
    def __init__(self, workspace_manager, root, *, objects=None):
        self.workspaces = workspace_manager
        self.factory = workspace_manager.factory
        excluded = (
            workspace_manager.root,
            *(g.repository for g in workspace_manager.grants.values()),
        )
        self.objects = objects or LocalObjects(root, excluded)

    def _event(self, session, actor, run_id, event, row=None, **payload):
        session.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=row.version if row else 0,
                event_type="artifact." + event,
                source="artifact_store",
                actor_id=actor.id,
                reason=event,
                payload={**payload, **({"artifact_id": row.id} if row else {})},
            )
        )

    def _audit(self, actor, run_id, code):
        try:
            with self.factory.begin() as session:
                self._event(session, actor, run_id, "denied", code=code)
        except SQLAlchemyError:
            # Do not pretend an unavailable database durably recorded an audit.
            raise ArtifactError(503, "artifact_database_and_audit_unavailable") from None

    def _guard(self, actor, run_id, function):
        try:
            return function()
        except DomainError as error:
            self._audit(actor, run_id, error.code)
            raise
        except SQLAlchemyError:
            self._audit(actor, run_id, "artifact_database_failed")
            raise ArtifactError(503, "artifact_database_failed") from None
        except (OSError, ValueError, UnicodeError):
            self._audit(actor, run_id, "artifact_storage_failed")
            raise ArtifactError(503, "artifact_storage_failed") from None

    def _binding(self, session, actor, run_id, workspace_id, sandbox_id, token):
        self.workspaces._authorize(session, actor, run_id, token)
        workspace = session.get(GitWorkspace, workspace_id)
        sandbox = session.get(SandboxInstance, sandbox_id)
        if (
            not workspace
            or not sandbox
            or workspace.owner_id != actor.owner_id
            or sandbox.owner_id != actor.owner_id
            or workspace.run_id != run_id
            or sandbox.run_id != run_id
            or sandbox.workspace_id != workspace.id
            or workspace.state != "READY"
            or workspace.lease_hash != token_hash(token)
            or sandbox.lease_hash != token_hash(token)
        ):
            reject("artifact_binding_mismatch", 403)
        return workspace, sandbox

    def _execution(self, session, sandbox):
        events = session.scalars(
            select(WorkflowEvent)
            .where(
                WorkflowEvent.aggregate_id == sandbox.run_id,
                WorkflowEvent.owner_id == sandbox.owner_id,
                WorkflowEvent.source == "sandbox_runtime",
            )
            .order_by(WorkflowEvent.occurred_at)
        )
        started, finished = None, None
        for event in events:
            if event.payload.get("sandbox_id") != sandbox.id:
                continue
            if event.event_type == "sandbox.running" and started is None:
                started = utc(event.occurred_at)
            if event.event_type in {"sandbox.completed", "sandbox.failed"}:
                finished = utc(event.occurred_at)
        status = "unknown"
        if sandbox.result is not None:
            status = "succeeded" if sandbox.result.get("exit_code") == 0 else "failed"
        elif sandbox.state == "FAILED":
            status = "failed"
        return ExecutionSnapshot(
            controller_state=sandbox.state,
            controller_version=sandbox.version,
            status=status,
            started_at=started,
            finished_at=finished,
        )

    def _state(self, session, actor, row, state, code=None):
        row.state, row.error_code = state, code
        row.version += 1
        self._event(session, actor, row.run_id, state.lower(), row, code=code)

    def _metadata(self, row):
        try:
            if row.schema_version != 1 or metadata_hash(row) != row.metadata_sha256:
                raise ValueError("changed metadata")
            metadata = ImmutableArtifact.model_validate_json(canonical(row.evidence))
            if (
                metadata.artifact_id != row.id
                or metadata.run_id != row.run_id
                or metadata.workspace_id != row.workspace_id
                or metadata.sandbox_id != row.sandbox_id
                or metadata.sha256 != row.sha256
                or metadata.size_bytes != row.size_bytes
                or metadata.producer_id != row.producer_id
            ):
                raise ValueError("metadata binding changed")
            return metadata
        except (ValueError, ValidationError, TypeError):
            raise IntegrityError(409, "artifact_metadata_corrupt") from None

    def _view(self, row, integrity="not_checked"):
        failed = row.state in {"FAILED", "CORRUPT"}
        return EvidenceDocument(
            artifact=self._metadata(row),
            storage_state=row.state,
            assessment=Assessment(
                status="failed" if failed else "unverified",
                reason="integrity_failed"
                if row.state == "CORRUPT"
                else "storage_failed"
                if failed
                else "no_trusted_verifier",
            ),
            integrity="failed" if row.state == "CORRUPT" else integrity,
        )

    def _key_row(self, session, actor, run_id, key_hash):
        row = session.scalar(
            select(ArtifactMetadata).where(
                ArtifactMetadata.owner_id == actor.owner_id,
                ArtifactMetadata.run_id == run_id,
                ArtifactMetadata.producer_id == actor.id,
                ArtifactMetadata.idempotency_hash == key_hash,
            )
        )
        if row:
            session.execute(
                update(ArtifactMetadata)
                .where(ArtifactMetadata.id == row.id)
                .values(version=ArtifactMetadata.version)
            )
            session.refresh(row)
        return row

    def put(
        self, actor, run_id, workspace_id, sandbox_id, lease_token, idempotency_key, upload, content
    ):
        return self._guard(
            actor,
            run_id,
            lambda: self._put(
                actor,
                run_id,
                workspace_id,
                sandbox_id,
                lease_token,
                idempotency_key,
                upload,
                content,
            ),
        )

    def _put(self, actor, run_id, workspace_id, sandbox_id, token, key, upload, content):
        if type(content) is not bytes or len(content) > MAX_BYTES:
            reject("invalid_artifact_bytes", 413)
        if not isinstance(upload, ArtifactUpload):
            reject("invalid_artifact_upload", 400)
        # Revalidate even a Pydantic model_construct/copy supplied by internal code.
        try:
            upload = ArtifactUpload.model_validate(upload.model_dump())
        except ValidationError:
            reject("invalid_artifact_upload", 400)
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
            reject("invalid_artifact_key", 400)
        key_hash = token_hash(key)
        checksum = hashlib.sha256(content).hexdigest()
        fingerprint = digest(
            {
                "run": run_id,
                "workspace": workspace_id,
                "sandbox": sandbox_id,
                "sha256": checksum,
                "size": len(content),
                "upload": upload.model_dump(),
            }
        )
        try:
            with self.factory.begin() as session:
                workspace, sandbox = self._binding(
                    session, actor, run_id, workspace_id, sandbox_id, token
                )
                row = self._key_row(session, actor, run_id, key_hash)
                if row is None:
                    row = ArtifactMetadata(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        sandbox_id=sandbox_id,
                        owner_id=actor.owner_id,
                        producer_id=actor.id,
                        lease_hash=token_hash(token),
                        idempotency_hash=key_hash,
                        request_hash=fingerprint,
                        sha256=checksum,
                        size_bytes=len(content),
                        evidence={},
                        metadata_sha256="",
                    )
                    session.add(row)
                    session.flush()
                    metadata = ImmutableArtifact(
                        artifact_id=row.id,
                        run_id=run_id,
                        workspace_id=workspace_id,
                        sandbox_id=sandbox_id,
                        sha256=checksum,
                        size_bytes=len(content),
                        **upload.model_dump(),
                        producer_id=actor.id,
                        created_at=now(),
                        base_commit=workspace.base_commit,
                        sandbox_input_sha256=sandbox.snapshot_sha256,
                        execution=self._execution(session, sandbox),
                    )
                    row.evidence = metadata.model_dump(mode="json")
                    row.metadata_sha256 = metadata_hash(row)
                    self._event(session, actor, run_id, "pending", row)
        except DatabaseIntegrityError:
            # Only a competing exact key is recoverable; other constraints must surface.
            with self.factory() as session:
                if (
                    session.scalar(
                        select(ArtifactMetadata.id).where(
                            ArtifactMetadata.run_id == run_id,
                            ArtifactMetadata.producer_id == actor.id,
                            ArtifactMetadata.idempotency_hash == key_hash,
                        )
                    )
                    is None
                ):
                    raise
        error = None
        with self.factory.begin() as session:
            self._binding(session, actor, run_id, workspace_id, sandbox_id, token)
            row = self._key_row(session, actor, run_id, key_hash)
            if row is None:
                reject("artifact_intent_missing")
            if row.request_hash != fingerprint or row.lease_hash != token_hash(token):
                reject("artifact_request_conflict")
            if row.state not in {"PENDING", "READY"}:
                reject("artifact_not_publishable")
            try:
                self._metadata(row)
                if row.state == "PENDING":
                    self.objects.publish(row.id, content, checksum)
                    self._state(session, actor, row, "READY")
                else:
                    self.objects.read(row.id, checksum, len(content))
                    self._event(session, actor, run_id, "replayed", row)
                self._binding(session, actor, run_id, workspace_id, sandbox_id, token)
                result = self._view(row, "valid")
            except (DomainError, OSError) as failure:
                error = (
                    failure.code if isinstance(failure, DomainError) else "artifact_storage_failed"
                )
                self._state(
                    session,
                    actor,
                    row,
                    "CORRUPT" if isinstance(failure, IntegrityError) else "FAILED",
                    error,
                )
        if error:
            reject(error)
        return result

    def _get(self, session, actor, run_id, artifact_id, token):
        self.workspaces._authorize(session, actor, run_id, token, cleanup=actor.role == "human")
        # Validate identity without touching storage before authorization.
        from uuid import UUID

        try:
            if not isinstance(artifact_id, str) or str(UUID(artifact_id)) != artifact_id:
                reject("invalid_artifact_id", 403)
        except (ValueError, AttributeError):
            reject("invalid_artifact_id", 403)
        row = session.scalar(
            select(ArtifactMetadata).where(
                ArtifactMetadata.id == artifact_id,
                ArtifactMetadata.run_id == run_id,
                ArtifactMetadata.owner_id == actor.owner_id,
            )
        )
        if row is None:
            reject("artifact_not_found", 404)
        session.execute(
            update(ArtifactMetadata)
            .where(ArtifactMetadata.id == row.id)
            .values(version=ArtifactMetadata.version)
        )
        session.refresh(row)
        self.workspaces._authorize(session, actor, run_id, token, cleanup=actor.role == "human")
        if actor.role == "worker" and (
            row.producer_id != actor.id or row.lease_hash != token_hash(token)
        ):
            reject("artifact_lease_changed", 403)
        return row

    def read(self, actor, run_id, artifact_id, lease_token=None):
        return self._guard(
            actor, run_id, lambda: self._read(actor, run_id, artifact_id, lease_token, True)
        )

    def describe(self, actor, run_id, artifact_id, lease_token=None):
        return self._guard(
            actor, run_id, lambda: self._read(actor, run_id, artifact_id, lease_token, False)
        )

    def _read(self, actor, run_id, artifact_id, token, content):
        error = None
        with self.factory.begin() as session:
            row = self._get(session, actor, run_id, artifact_id, token)
            try:
                if not content:
                    self._event(session, actor, run_id, "described", row)
                    return self._view(row)
                if row.state != "READY":
                    reject("artifact_not_readable")
                self._metadata(row)
                data = self.objects.read(row.id, row.sha256, row.size_bytes)
                self.workspaces._authorize(
                    session, actor, run_id, token, cleanup=actor.role == "human"
                )
                result = ArtifactRead(self._view(row, "valid"), data)
                self._event(session, actor, run_id, "read", row)
            except IntegrityError as failure:
                error = failure.code
                self._state(session, actor, row, "CORRUPT", error)
        if error:
            reject(error)
        return result
