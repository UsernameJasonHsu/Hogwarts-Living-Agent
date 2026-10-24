"""Typed internal patch build records; applicability is never a QA assessment."""

from typing import Annotated, Literal, Protocol

from pydantic import Field

from hogwarts.artifacts.contracts import FrozenModel, Identity, Sha256
from hogwarts.auth import Principal
from hogwarts.service import DomainError

VERSION = "trusted-git-patch-builder.v1"
MAX_PATCH = 16 * 1024 * 1024


class PatchError(DomainError):
    pass


def deny(code, status=409):
    raise PatchError(status, code)


class ImportedFile(FrozenModel):
    source_path: str
    target_path: str
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0, le=4 * 1024 * 1024)]
    artifact_id: Identity


class ImportManifest(FrozenModel):
    schema_version: Literal[1]
    import_id: Identity
    run_id: Identity
    workspace_id: Identity
    sandbox_id: Identity
    assessment: Literal["unverified"]
    archive_artifact_id: Identity
    archive_sha256: Sha256
    capture_proof: dict
    files: Annotated[list[ImportedFile], Field(max_length=256)]


class BuildProof(FrozenModel):
    builder_version: Literal["trusted-git-patch-builder.v1"] = VERSION
    base_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    base_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    result_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    git_version: str
    import_manifest_sha256: Sha256
    changed_paths: list[str]
    source_files: list[ImportedFile]
    applicability: Literal["applicable", "empty_no_changes"]
    assessment: Literal["unverified"] = "unverified"


class BuildJournal(FrozenModel):
    schema_version: Literal[1] = 1
    build_id: Identity
    run_id: Identity
    import_id: Identity
    workspace_id: Identity
    sandbox_id: Identity
    state: Literal["PENDING", "BUILDING", "BUILT", "PUBLISHING", "READY", "FAILED"] = "PENDING"
    version: Annotated[int, Field(ge=1)] = 1
    patch_sha256: Sha256 | None = None
    proof: BuildProof | None = None
    proof_sha256: Sha256 | None = None
    patch_artifact_id: Identity | None = None
    proof_artifact_id: Identity | None = None
    error_code: str | None = None


class PatchInfo(FrozenModel):
    build_id: Identity
    run_id: Identity
    import_id: Identity
    patch_artifact_id: Identity
    proof_artifact_id: Identity
    patch_sha256: Sha256
    changed_paths: list[str]
    applicability: Literal["applicable", "empty_no_changes"]
    assessment: Literal["unverified"] = "unverified"


class TrustedGitPatchBuilder(Protocol):
    def build(
        self, actor: Principal, run_id: str, import_id: str, lease_token: str
    ) -> PatchInfo: ...
