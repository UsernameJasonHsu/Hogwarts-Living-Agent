"""Validate imported immutable input before any private Git reconstruction."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter
from sqlalchemy import select

from hogwarts.artifacts.contracts import Identity
from hogwarts.models import ArtifactMetadata, OutputImport
from hogwarts.outputs.archive import validate_archive
from hogwarts.outputs.capsule import canonical
from hogwarts.patches.contracts import VERSION, ImportManifest, deny
from hogwarts.service import digest
from hogwarts.workspaces.manager import token_hash


@dataclass(frozen=True)
class Context:
    import_id: str
    run_id: str
    workspace_id: str
    sandbox_id: str
    base_commit: str
    manifest_id: str
    manifest_sha256: str
    archive_id: str
    archive_sha256: str
    prefix: str
    allowed: tuple[str, ...]
    repository: Path
    files: tuple
    proof: dict
    fingerprint: str


@dataclass(frozen=True)
class Inputs:
    context: Context
    manifest: ImportManifest
    contents: dict[str, bytes]


class InputValidator:
    def __init__(self, importer, git_version):
        self.importer, self.store, self.workspaces = importer, importer.store, importer.workspaces
        self.factory = importer.factory
        self.git_version = git_version

    def context(self, session, actor, run_id, import_id, token):
        self.workspaces._authorize(session, actor, run_id, token)
        TypeAdapter(Identity).validate_python(import_id)
        row = session.get(OutputImport, import_id)
        if (
            not row
            or row.run_id != run_id
            or row.owner_id != actor.owner_id
            or row.producer_id != actor.id
            or row.lease_hash != token_hash(token)
            or row.state != "IMPORTED"
        ):
            deny("patch_import_binding_mismatch", 403)
        sandbox, _, grant, allowed, import_hash = self.importer._binding(
            session, actor, run_id, row.workspace_id, row.sandbox_id, token
        )
        if (
            row.request_hash != import_hash
            or not row.manifest_artifact_id
            or not row.manifest_sha256
        ):
            deny("patch_import_not_complete")
        from hogwarts.models import GitWorkspace

        ws = session.get(GitWorkspace, row.workspace_id)

        def artifact(key, identifier=None):
            result = session.scalar(
                select(ArtifactMetadata).where(
                    ArtifactMetadata.owner_id == actor.owner_id,
                    ArtifactMetadata.run_id == run_id,
                    ArtifactMetadata.producer_id == actor.id,
                    ArtifactMetadata.idempotency_hash
                    == token_hash("oi_" + import_id.replace("-", "") + "_" + key),
                )
            )
            if (
                not result
                or (identifier and result.id != identifier)
                or result.workspace_id != ws.id
                or result.sandbox_id != sandbox.id
                or result.lease_hash != token_hash(token)
                or result.state != "READY"
                or result.evidence.get("kind") != ("report" if key == "manifest" else "binary")
                or result.evidence.get("media_type")
                != ("application/json" if key == "manifest" else "application/octet-stream")
            ):
                deny("patch_source_artifact_binding", 403)
            return result

        manifest = artifact("manifest", row.manifest_artifact_id)
        raw = artifact("archive")
        if (
            manifest.sha256 != row.manifest_sha256
            or raw.sha256 != row.archive_sha256
            or manifest.evidence.get("kind") != "report"
            or manifest.evidence.get("media_type") != "application/json"
        ):
            deny("patch_source_metadata_mismatch")
        files = tuple(row.files)
        for index, file in enumerate(files):
            record = artifact(str(index), file.get("artifact_id"))
            if record.sha256 != file.get("sha256") or record.size_bytes != file.get("size_bytes"):
                deny("patch_source_metadata_mismatch")
        fingerprint = digest(
            {
                "builder": VERSION,
                "import_id": row.id,
                "run_id": run_id,
                "owner": actor.owner_id,
                "producer": actor.id,
                "import_request": import_hash,
                "manifest_id": manifest.id,
                "manifest_sha256": manifest.sha256,
                "archive_id": raw.id,
                "archive_sha256": raw.sha256,
                "base_commit": ws.base_commit,
                "files": files,
                "proof": row.proof,
                "git_version": self.git_version,
            }
        )
        return Context(
            row.id,
            run_id,
            ws.id,
            sandbox.id,
            ws.base_commit,
            manifest.id,
            manifest.sha256,
            raw.id,
            raw.sha256,
            grant.target_prefix,
            tuple(allowed),
            Path(ws.repository_path),
            files,
            row.proof,
            fingerprint,
        )

    def load(self, actor, run_id, import_id, token):
        with self.factory() as session:
            c = self.context(session, actor, run_id, import_id, token)

        def read(identifier, sha):
            result = self.store.read(actor, run_id, identifier, token)
            meta = result.evidence.artifact
            if (
                meta.run_id != run_id
                or meta.workspace_id != c.workspace_id
                or meta.sandbox_id != c.sandbox_id
                or meta.base_commit != c.base_commit
                or meta.producer_id != actor.id
                or meta.sha256 != sha
                or result.evidence.assessment.status != "unverified"
                or hashlib.sha256(result.content).hexdigest() != sha
                or meta.sandbox_input_sha256 != c.proof.get("sandbox_input_sha256")
            ):
                deny("patch_artifact_provenance_mismatch")
            return result.content

        raw_manifest = read(c.manifest_id, c.manifest_sha256)
        manifest = ImportManifest.model_validate_json(raw_manifest)
        expected = {
            "schema_version": 1,
            "import_id": c.import_id,
            "run_id": run_id,
            "workspace_id": c.workspace_id,
            "sandbox_id": c.sandbox_id,
            "assessment": "unverified",
            "archive_artifact_id": c.archive_id,
            "archive_sha256": c.archive_sha256,
            "capture_proof": c.proof,
            "files": list(c.files),
        }
        if canonical(manifest.model_dump(mode="json")) != canonical(
            expected
        ) or raw_manifest != canonical(expected):
            deny("patch_manifest_mismatch")
        archive = read(c.archive_id, c.archive_sha256)
        parsed = validate_archive(archive, c.prefix, c.allowed)
        if len(parsed) != len(manifest.files):
            deny("patch_file_set_mismatch")
        contents = {}
        for output, record in zip(parsed, manifest.files, strict=True):
            if (
                output.source_path != record.source_path
                or output.target_path != record.target_path
                or output.sha256 != record.sha256
                or len(output.content) != record.size_bytes
                or record.target_path in contents
            ):
                deny("patch_file_record_mismatch")
            data = read(record.artifact_id, record.sha256)
            if data != output.content or hashlib.sha256(data).hexdigest() != record.sha256:
                deny("patch_file_content_mismatch")
            contents[record.target_path] = data
        with self.factory() as session:
            if self.context(session, actor, run_id, import_id, token).fingerprint != c.fingerprint:
                deny("patch_inputs_changed")
        return Inputs(c, manifest, contents)
