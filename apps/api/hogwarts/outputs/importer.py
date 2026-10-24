"""Trusted controller facade, authenticated fixed snapshot and immutable artifact publication."""

import hashlib
import secrets
from dataclasses import asdict

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from hogwarts.artifacts.contracts import ArtifactUpload
from hogwarts.models import OutputImport, WorkflowEvent
from hogwarts.outputs.archive import validate_archive
from hogwarts.outputs.capsule import CapsuleVault, canonical
from hogwarts.outputs.contracts import ImportInfo, OutputError, deny
from hogwarts.service import DomainError, digest
from hogwarts.workspaces.git import relative_path
from hogwarts.workspaces.manager import token_hash


class LocalOutputImporter:
    def __init__(self, controller, store, root, grants):
        if controller.workspaces is not store.workspaces:
            deny("output_controller_store_mismatch")
        self.controller, self.store = controller, store
        self.workspaces, self.factory = store.workspaces, store.factory
        self.grants = {g.profile_name: g for g in grants}
        if len(self.grants) != len(grants):
            deny("duplicate_output_grant")
        for grant in grants:
            relative_path(grant.target_prefix)
        self.vault = CapsuleVault(
            root,
            (
                store.objects.root,
                self.workspaces.root,
                *(g.repository for g in self.workspaces.grants.values()),
            ),
        )

    def _event(self, session, actor, run_id, event, row=None, **payload):
        session.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=row.version if row else 0,
                event_type="output_import." + event,
                source="secure_output_import",
                actor_id=actor.id,
                reason=event,
                payload={
                    **payload,
                    **({"import_id": row.id, "sandbox_id": row.sandbox_id} if row else {}),
                },
            )
        )

    def _state(self, session, actor, row, state, code=None):
        row.state, row.error_code = state, code
        row.version += 1
        self._event(session, actor, row.run_id, state.lower(), row, code=code)

    def _binding(self, session, actor, run_id, workspace_id, sandbox_id, token):
        workspace, sandbox = self.store._binding(
            session, actor, run_id, workspace_id, sandbox_id, token
        )
        _, proposal, project = self.workspaces._authorize(session, actor, run_id, token)
        repo, allowed, commit, fingerprint, _ = self.workspaces._configuration(proposal, project)
        if (
            str(repo) != workspace.repository_path
            or commit != workspace.base_commit
            or list(allowed) != workspace.allowed_paths
            or fingerprint != workspace.request_hash
        ):
            deny("output_workspace_configuration_changed", 403)
        self.workspaces.git.provenance(repo, self.workspaces._path(workspace), commit)
        grant = self.grants.get(sandbox.profile_name)
        profile = self.controller.profiles.get(sandbox.profile_name)
        if not grant or not profile or digest(asdict(profile)) != sandbox.request_hash:
            deny("output_profile_not_granted", 403)
        existing = session.scalar(select(OutputImport).where(OutputImport.sandbox_id == sandbox_id))
        sealed_replay = (
            sandbox.state == "DESTROYED"
            and existing is not None
            and existing.state in {"SNAPSHOT_READY", "IMPORTING", "IMPORTED"}
        )
        if (
            (sandbox.state != "COMPLETED" and not sealed_replay)
            or not sandbox.result
            or sandbox.result.get("exit_code") != 0
        ):
            deny("output_execution_not_completed")
        fingerprint = digest(
            {
                "run": run_id,
                "workspace": workspace_id,
                "sandbox": sandbox_id,
                "lease": token_hash(token),
                "producer": actor.id,
                "configuration": fingerprint,
                "prefix": grant.target_prefix,
                "profile": digest(asdict(profile)),
                "reader_image": self.controller.driver.reader_image,
                "invocation": sandbox.invocation_id,
                "container": sandbox.container_id,
                "input_sha256": sandbox.snapshot_sha256,
            }
        )
        return sandbox, profile, grant, allowed, fingerprint

    def _row(self, session, sandbox_id):
        row = session.scalar(select(OutputImport).where(OutputImport.sandbox_id == sandbox_id))
        if row:
            session.execute(
                update(OutputImport)
                .where(OutputImport.id == row.id)
                .values(version=OutputImport.version)
            )
            session.refresh(row)
        return row

    def import_output(self, controller, actor, run_id, workspace_id, sandbox_id, lease_token):
        try:
            if controller is not self.controller:
                deny("trusted_output_controller_required", 403)
            return self._import(actor, run_id, workspace_id, sandbox_id, lease_token)
        except (DomainError, SQLAlchemyError, OSError, ValueError, TypeError) as error:
            code = (
                error.code
                if isinstance(error, DomainError)
                else "output_database_failed"
                if isinstance(error, SQLAlchemyError)
                else "output_storage_failed"
            )
            try:
                with self.factory.begin() as session:
                    self._event(session, actor, run_id, "denied", code=code)
            except SQLAlchemyError:
                raise OutputError(503, "output_database_and_audit_unavailable") from None
            raise OutputError(
                503
                if isinstance(error, (SQLAlchemyError, OSError))
                else getattr(error, "status", 409),
                code,
            ) from None

    def _import(self, actor, run_id, workspace_id, sandbox_id, token):
        try:
            with self.factory.begin() as session:
                *_, fingerprint = self._binding(
                    session, actor, run_id, workspace_id, sandbox_id, token
                )
                if self._row(session, sandbox_id) is None:
                    row = OutputImport(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        sandbox_id=sandbox_id,
                        owner_id=actor.owner_id,
                        producer_id=actor.id,
                        lease_hash=token_hash(token),
                        request_hash=fingerprint,
                        seal_key=secrets.token_hex(32),
                    )
                    session.add(row)
                    session.flush()
                    self._event(session, actor, run_id, "pending", row)
        except IntegrityError:
            # Unique sandbox intent; exact binding is checked under its SQL lock below.
            with self.factory() as session:
                if not session.scalar(
                    select(OutputImport.id).where(OutputImport.sandbox_id == sandbox_id)
                ):
                    raise
        # Commit capture intent BEFORE external Docker/filesystem IO.
        with self.factory.begin() as session:
            _, _, _, _, fingerprint = self._binding(
                session, actor, run_id, workspace_id, sandbox_id, token
            )
            row = self._row(session, sandbox_id)
            if row.request_hash != fingerprint:
                deny("output_request_conflict")
            if row.state == "PENDING":
                self._state(session, actor, row, "SNAPSHOTTING")
                capture = True
            else:
                capture = False
        error = None
        with self.factory.begin() as session:
            sandbox, profile, grant, allowed, fingerprint = self._binding(
                session, actor, run_id, workspace_id, sandbox_id, token
            )
            # Serialize with controller execute/stop/destroy on the SAME sandbox row.
            from hogwarts.models import SandboxInstance

            session.execute(
                update(SandboxInstance)
                .where(SandboxInstance.id == sandbox_id)
                .values(version=SandboxInstance.version)
            )
            session.refresh(sandbox)
            self._binding(session, actor, run_id, workspace_id, sandbox_id, token)
            row = self._row(session, sandbox_id)
            if row.request_hash != fingerprint or row.state in {"REJECTED", "FAILED", "UNCERTAIN"}:
                deny("output_not_importable")
            if not capture and not self.vault.exists(row):
                deny("output_capture_busy_or_uncertain")
            try:
                if self.vault.exists(row):
                    archive, proof = self.vault.open(row)
                elif capture and row.state == "SNAPSHOTTING":
                    archive, proof = self.controller.driver.export_output(sandbox, profile)
                    archive, proof = self.vault.seal(row, archive, proof)
                else:
                    deny("output_snapshot_uncertain")
                expected = {
                    "container_id": sandbox.container_id,
                    "invocation_id": sandbox.invocation_id,
                    "profile_hash": sandbox.request_hash,
                    "sandbox_input_sha256": sandbox.snapshot_sha256,
                    "reader_image": self.controller.driver.reader_image,
                }
                if any(proof.get(k) != v for k, v in expected.items()):
                    deny("output_capture_binding_invalid")
                files = validate_archive(archive, grant.target_prefix, allowed)
                sha = hashlib.sha256(archive).hexdigest()
                if row.archive_sha256 and (row.archive_sha256 != sha or row.proof != proof):
                    deny("output_snapshot_changed")
                row.archive_sha256, row.proof = sha, proof
                if row.state in {"SNAPSHOTTING", "PENDING"}:
                    self._state(session, actor, row, "SNAPSHOT_READY")
                identifier = row.id
            except (DomainError, OSError, ValueError) as failure:
                error = failure
                self._state(
                    session,
                    actor,
                    row,
                    "UNCERTAIN" if row.state == "SNAPSHOTTING" else "FAILED",
                    failure.code if isinstance(failure, DomainError) else "output_capture_failed",
                )
        if error:
            raise error
        # No import SQL write lock is held across ArtifactStore's independent transactions.
        with self.factory.begin() as session:
            self._binding(session, actor, run_id, workspace_id, sandbox_id, token)
            row = self._row(session, sandbox_id)
            if row.state not in {"SNAPSHOT_READY", "IMPORTING", "IMPORTED"}:
                deny("output_not_publishable")
            if row.state == "SNAPSHOT_READY":
                self._state(session, actor, row, "IMPORTING")

        def put(key, content, kind="binary", media="application/octet-stream"):
            with self.factory() as session:
                self._binding(session, actor, run_id, workspace_id, sandbox_id, token)
            return self.store.put(
                actor,
                run_id,
                workspace_id,
                sandbox_id,
                token,
                "oi_" + identifier.replace("-", "") + "_" + key,
                ArtifactUpload(kind=kind, media_type=media),
                content,
            ).artifact

        raw = put("archive", archive)
        records = []
        for index, file in enumerate(files):
            artifact = put(str(index), file.content)
            records.append(
                {
                    "source_path": file.source_path,
                    "target_path": file.target_path,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "artifact_id": artifact.artifact_id,
                }
            )
        manifest = canonical(
            {
                "schema_version": 1,
                "import_id": identifier,
                "run_id": run_id,
                "workspace_id": workspace_id,
                "sandbox_id": sandbox_id,
                "assessment": "unverified",
                "archive_artifact_id": raw.artifact_id,
                "archive_sha256": raw.sha256,
                "capture_proof": proof,
                "files": records,
            }
        )
        published = put("manifest", manifest, "report", "application/json")
        with self.factory.begin() as session:
            self._binding(session, actor, run_id, workspace_id, sandbox_id, token)
            row = self._row(session, sandbox_id)
            if row.state not in {"IMPORTING", "IMPORTED"} or row.request_hash != fingerprint:
                deny("output_finalize_conflict")
            if row.manifest_artifact_id and (
                row.manifest_artifact_id != published.artifact_id
                or row.manifest_sha256 != published.sha256
                or row.files != records
            ):
                deny("output_manifest_changed")
            row.files, row.manifest_artifact_id, row.manifest_sha256 = (
                records,
                published.artifact_id,
                published.sha256,
            )
            self._state(session, actor, row, "IMPORTED")
            return ImportInfo(
                row.id,
                run_id,
                workspace_id,
                sandbox_id,
                row.state,
                row.archive_sha256,
                row.manifest_artifact_id,
                row.manifest_sha256,
                len(records),
            )
