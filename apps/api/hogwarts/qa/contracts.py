"""Versioned execution facts independent of unverified Artifact content."""

from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from hogwarts.artifacts.contracts import FrozenModel, Identity, Sha256
from hogwarts.auth import Principal
from hogwarts.service import DomainError

MAX_LOG = 262144
VERSION = "trusted-qa-runner.v1"
Outcome = Literal["pass", "fail", "timeout", "infrastructure_error", "unknown_execution"]


class QAError(DomainError):
    pass


def deny(code, status=409):
    raise QAError(status, code)


class ExecutionResult(FrozenModel):
    outcome: Outcome
    exit_code: int | None
    stdout: Annotated[str, Field(max_length=MAX_LOG * 2, pattern=r"^(?:[0-9a-f]{2})*$")]
    stderr: Annotated[str, Field(max_length=MAX_LOG * 2, pattern=r"^(?:[0-9a-f]{2})*$")]
    started_at: str
    finished_at: str
    container_id: str | None
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    oom_killed: bool = False
    timed_out: bool = False
    cleanup_complete: bool = False
    error_code: str | None = None

    @model_validator(mode="after")
    def valid_observation(self):
        start, finish = (
            datetime.fromisoformat(self.started_at),
            datetime.fromisoformat(self.finished_at),
        )
        if start.tzinfo is None or finish.tzinfo is None or finish < start:
            raise ValueError("invalid execution interval")
        if self.outcome == "pass" and (
            self.exit_code != 0 or self.oom_killed or self.timed_out or not self.container_id
        ):
            raise ValueError("pass requires observed successful command")
        if self.outcome == "fail" and (self.exit_code is None or self.exit_code == 0):
            raise ValueError("fail requires nonzero exit")
        return self


class LogRef(FrozenModel):
    artifact_id: Identity
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0, le=MAX_LOG)]


class QAExecutionAttestation(FrozenModel):
    schema_version: Literal[1] = 1
    issuer: Literal["trusted-qa-runner.v1"] = VERSION
    authority: Literal["controller_journal_and_hmac_capsule"] = (
        "controller_journal_and_hmac_capsule"
    )
    qa_id: Identity
    run_id: Identity
    worker_id: str
    controller_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
    workspace_id: Identity
    original_sandbox_id: Identity
    qa_sandbox_id: Identity
    build_id: Identity
    patch_artifact_id: Identity
    patch_sha256: Sha256
    build_proof_artifact_id: Identity
    build_proof_sha256: Sha256
    base_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    result_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    profile_name: str
    profile_sha256: Sha256
    argv: list[str]
    container_argv: list[str]
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    input_sha256: Sha256
    resource_limits: dict
    container_id: str | None
    outcome: Outcome
    exit_code: int | None
    started_at: str
    finished_at: str
    oom_killed: bool
    timed_out: bool
    cleanup_complete: bool
    error_code: str | None
    stdout: LogRef
    stderr: LogRef
    content_assessment: Literal["unverified"] = "unverified"


class QAJournal(FrozenModel):
    schema_version: Literal[1] = 1
    qa_id: Identity
    run_id: Identity
    build_id: Identity
    import_id: Identity
    workspace_id: Identity
    sandbox_id: Identity
    worker_id: str
    controller_id: Annotated[str, Field(pattern=r"^[a-z0-9-]{1,40}$")]
    lease_hash: Sha256
    request_hash: Sha256
    profile_name: str
    profile_sha256: Sha256
    seal_key: Sha256
    expires_at: str
    state: Literal["PENDING", "EXECUTING", "CAPTURED", "PUBLISHING", "READY"] = "PENDING"
    version: Annotated[int, Field(ge=1)] = 1
    container_id: str | None = None
    input_sha256: Sha256
    result_sha256: Sha256 | None = None
    outcome: Outcome | None = None
    attestation_artifact_id: Identity | None = None
    attestation_sha256: Sha256 | None = None
    cleanup_complete: bool = False
    cleanup_requested: bool = False


class QAInfo(FrozenModel):
    qa_id: Identity
    run_id: Identity
    outcome: Outcome
    attestation_artifact_id: Identity
    attestation_sha256: Sha256


class TrustedQARunner(Protocol):
    def run(
        self, actor: Principal, run_id: str, build_id: str, profile_name: str, lease_token: str
    ) -> QAInfo: ...
    def cleanup(
        self, actor: Principal, run_id: str, qa_id: str, lease_token: str | None = None
    ) -> None: ...
