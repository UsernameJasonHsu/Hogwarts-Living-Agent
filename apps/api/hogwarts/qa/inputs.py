"""Validate existing patch facts and materialize data only, never execute source."""

import hashlib
import io
import tarfile
from dataclasses import dataclass

from pydantic import TypeAdapter

from hogwarts.artifacts.contracts import Identity
from hogwarts.models import IdempotencyRecord
from hogwarts.outputs.capsule import canonical
from hogwarts.patches.contracts import BuildJournal
from hogwarts.patches.git import blob_id
from hogwarts.qa.contracts import VERSION, deny
from hogwarts.qa.profiles import safe_source_path
from hogwarts.service import digest
from hogwarts.workspaces.git import within


@dataclass(frozen=True)
class QAInput:
    context: object
    build: BuildJournal
    archive: bytes
    fingerprint: str


class QAInputValidator:
    def __init__(self, builder, controller_id):
        self.builder = builder
        self.controller_id = controller_id
        self.factory = builder.factory

    def load(self, actor, run_id, build_id, token, profile):
        TypeAdapter(Identity).validate_python(build_id)
        b = self.builder
        with self.factory() as s:
            b.workspaces._authorize(s, actor, run_id, token)
            row = s.get(IdempotencyRecord, build_id)
            if not row or row.owner_id != actor.owner_id or row.actor_id != actor.id:
                deny("qa_build_not_authorized", 403)
            j = BuildJournal.model_validate(row.response)
            if (
                j.build_id != build_id
                or j.run_id != run_id
                or j.state != "READY"
                or row.key != b._key(j.import_id)
            ):
                deny("qa_build_not_ready", 403)
        inputs = b.validator.load(actor, run_id, j.import_id, token)
        c = inputs.context
        with self.factory() as s:
            locked = b._row(s, actor, j.import_id)
            if b._journal(locked, c) != j:
                deny("qa_build_changed")
        patch, proof = b._candidate(j, inputs)
        for identifier, expected, suffix in (
            (j.patch_artifact_id, patch, "patch"),
            (j.proof_artifact_id, canonical(proof.model_dump(mode="json")), "proof"),
        ):
            obj = b.store.read(actor, run_id, identifier, token)
            meta = obj.evidence.artifact
            from hogwarts.models import ArtifactMetadata
            from hogwarts.workspaces.manager import token_hash

            with self.factory() as s:
                stored = s.get(ArtifactMetadata, identifier)
                if not stored or stored.idempotency_hash != token_hash(
                    "pb_" + build_id.replace("-", "") + "_" + suffix
                ):
                    deny("qa_patch_key_mismatch")
            if (
                obj.content != expected
                or meta.run_id != run_id
                or meta.workspace_id != c.workspace_id
                or meta.sandbox_id != c.sandbox_id
                or meta.base_commit != c.base_commit
                or meta.producer_id != actor.id
                or meta.kind != ("patch" if suffix == "patch" else "report")
                or obj.evidence.assessment.status != "unverified"
            ):
                deny("qa_patch_binding_mismatch", 403)
        # Rebuild and compare rather than interpreting Agent-provided patch or proof as authority.
        actual_patch, actual_proof = b.git.construct(inputs)
        if actual_patch != patch or actual_proof != proof:
            deny("qa_patch_reconstruction_mismatch")
        if not within(profile.test_root, c.allowed):
            deny("qa_test_root_not_authorized", 403)
        tree = b.workspaces.git.tree(c.repository, c.base_commit)
        files = {}
        total = 0
        for path, (_, sha) in sorted(tree.items()):
            if not within(path, c.allowed):
                continue
            safe_source_path(path)
            if path in inputs.contents:
                continue
            size = int(b.workspaces.git.run(c.repository, "cat-file", "-s", sha))
            if size > 8 * 1024 * 1024 or total + size > 40 * 1024 * 1024:
                deny("qa_base_size_limit")
            data = b.workspaces.git.run(c.repository, "cat-file", "blob", sha)
            if len(data) != size or blob_id(data) != sha:
                deny("qa_base_blob_mismatch")
            total += len(data)
            files[path] = data
        for path, data in inputs.contents.items():
            safe_source_path(path)
            total += len(data)
            files[path] = data
        if total > 48 * 1024 * 1024 or len(files) > 10000:
            deny("qa_snapshot_limit")
        output = io.BytesIO()
        directories = set()
        with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for path, data in sorted(files.items()):
                parts = path.split("/")
                for n in range(1, len(parts)):
                    parent = "/".join(parts[:n])
                    if parent not in directories:
                        entry = tarfile.TarInfo(parent)
                        entry.type = tarfile.DIRTYPE
                        entry.mode = 0o555
                        tar.addfile(entry)
                        directories.add(parent)
                entry = tarfile.TarInfo(path)
                entry.size = len(data)
                entry.mode = 0o444
                tar.addfile(entry, io.BytesIO(data))
        archive = output.getvalue()
        if len(archive) > 60 * 1024 * 1024:
            deny("qa_snapshot_limit")
        fingerprint = digest(
            {
                "runner": VERSION,
                "controller_id": self.controller_id,
                "input": c.fingerprint,
                "build": j.model_dump(mode="json"),
                "profile": profile.fingerprint,
                "snapshot": hashlib.sha256(archive).hexdigest(),
            }
        )
        # Current authorization is checked again after bounded materialization.
        with self.factory() as s:
            b._context(s, actor, run_id, j.import_id, token, c)
        return QAInput(c, j, archive, fingerprint)
