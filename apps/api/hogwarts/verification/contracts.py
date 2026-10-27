from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from hogwarts.artifacts.contracts import FrozenModel, Identity, Sha256
from hogwarts.auth import Principal

Verdict = Literal["READY_FOR_HUMAN_REVIEW", "FAILED", "BLOCKED", "UNKNOWN"]


class EvidenceRef(FrozenModel):
    artifact_id: Identity
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0, le=16777216)]


class ReviewBundle(FrozenModel):
    schema_version: Literal[1] = 1
    issuer: Literal["prototype-verification.v1"] = "prototype-verification.v1"
    verification_id: Identity
    run_id: Identity
    workspace_id: Identity
    sandbox_id: Identity
    worker_id: str
    base_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    configuration_sha256: Sha256
    proposal_sha256: Sha256
    profile_name: str
    profile_sha256: Sha256
    image_id: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    build_id: Identity | None
    qa_id: Identity | None
    attestation_artifact_id: Identity | None
    patch_artifact_id: Identity
    build_proof_artifact_id: Identity
    input_sha256: Sha256
    result_tree: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    changed_paths: list[str]
    verdict: Verdict
    reasons: list[str]
    tests_executed: Annotated[int, Field(ge=0)]
    test_count_authority: Literal["approved_image_exit_protocol", "insufficient_evidence"]
    artifacts: list[EvidenceRef]
    created_at: str
    assessment: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def sufficient_evidence(self):
        identifiers = [ref.artifact_id for ref in self.artifacts]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("duplicate evidence references")
        if self.verdict == "READY_FOR_HUMAN_REVIEW" and (
            self.tests_executed != 2
            or self.test_count_authority != "approved_image_exit_protocol"
            or self.reasons
            or not self.changed_paths
            or not self.build_id
            or not self.qa_id
            or any(
                ref not in identifiers
                for ref in (
                    self.patch_artifact_id,
                    self.build_proof_artifact_id,
                    self.attestation_artifact_id,
                )
            )
        ):
            raise ValueError("ready requires complete positive trusted test evidence")
        return self


class VerificationJournal(FrozenModel):
    schema_version: Literal[1] = 1
    verification_id: Identity
    run_id: Identity
    worker_id: str
    lease_hash: Sha256
    request_hash: Sha256
    runtime_profile: str
    qa_profile: str
    profile_sha256: Sha256
    seal_key: Sha256
    workspace_id: Identity | None = None
    sandbox_id: Identity | None = None
    state: Literal[
        "PENDING", "EXECUTING", "CAPTURED", "PUBLISHING", "COMPLETE", "UNKNOWN", "BLOCKED"
    ] = "PENDING"
    version: Annotated[int, Field(ge=1)] = 1
    verdict: Verdict | None = None
    bundle_artifact_id: Identity | None = None
    bundle_sha256: Sha256 | None = None
    approval_id: Identity | None = None
    proposal_version: int | None = None
    error_code: str | None = None


class VerificationInfo(FrozenModel):
    verification_id: Identity
    run_id: Identity
    status: Verdict
    bundle_artifact_id: Identity | None
    bundle_sha256: Sha256 | None
    approval_id: Identity | None
    proposal_version: int | None


class PrototypeVerificationOrchestrator(Protocol):
    def verify(
        self,
        actor: Principal,
        run_id: str,
        lease_token: str,
        runtime_profile: str,
        qa_profile: str,
    ) -> VerificationInfo: ...

    def review(
        self,
        actor: Principal,
        run_id: str,
        verification_id: str,
        bundle_sha256: str,
        expected_version: int,
        decision: str,
        idempotency_key: str,
    ) -> dict: ...
