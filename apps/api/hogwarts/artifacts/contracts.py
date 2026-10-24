from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from hogwarts.auth import Principal
from hogwarts.service import DomainError

MAX_BYTES = 16 * 1024 * 1024
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ArtifactError(DomainError):
    pass


class IntegrityError(ArtifactError):
    """Detected unsafe path, metadata mismatch or missing/changed bytes."""


def reject(code, status=409):
    raise ArtifactError(status, code)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArtifactUpload(FrozenModel):
    kind: Literal["report", "patch", "log", "binary"]
    media_type: Literal["text/plain", "application/json", "application/octet-stream"]


class ExecutionSnapshot(FrozenModel):
    controller_state: Annotated[str, Field(min_length=1, max_length=30)]
    controller_version: Annotated[int, Field(ge=1)]
    status: Literal["unknown", "succeeded", "failed"]
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("execution times out of order")
        return self


class ImmutableArtifact(FrozenModel):
    artifact_id: Identity
    run_id: Identity
    workspace_id: Identity
    sandbox_id: Identity
    sha256: Sha256
    size_bytes: Annotated[int, Field(ge=0, le=MAX_BYTES)]
    kind: Literal["report", "patch", "log", "binary"]
    media_type: Literal["text/plain", "application/json", "application/octet-stream"]
    source: Literal["agent_submission"] = "agent_submission"
    producer_id: Annotated[str, Field(min_length=1, max_length=100)]
    created_at: AwareDatetime
    base_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    sandbox_input_sha256: Sha256
    execution: ExecutionSnapshot


class Assessment(FrozenModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {"properties": {"status": {"const": "verified"}}},
                    "then": {
                        "properties": {
                            "reason": {"const": "trusted_verifier"},
                            "verifier_ref": {"type": "string", "minLength": 1},
                        },
                        "required": ["verifier_ref"],
                    },
                },
                {
                    "if": {"properties": {"status": {"const": "unverified"}}},
                    "then": {
                        "properties": {
                            "reason": {"const": "no_trusted_verifier"},
                            "verifier_ref": {"type": "null"},
                        }
                    },
                },
                {
                    "if": {"properties": {"status": {"const": "failed"}}},
                    "then": {
                        "properties": {
                            "reason": {"enum": ["storage_failed", "integrity_failed"]},
                            "verifier_ref": {"type": "null"},
                        }
                    },
                },
            ]
        }
    )
    status: Literal["verified", "unverified", "failed"]
    reason: Literal["no_trusted_verifier", "storage_failed", "integrity_failed", "trusted_verifier"]
    verifier_ref: Annotated[str, Field(min_length=1, max_length=100)] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "verified":
            if self.reason != "trusted_verifier" or not self.verifier_ref:
                raise ValueError("verified requires a trusted verifier reference")
        elif self.status == "unverified":
            if self.reason != "no_trusted_verifier" or self.verifier_ref:
                raise ValueError("unverified has no verifier")
        elif self.reason not in {"storage_failed", "integrity_failed"} or self.verifier_ref:
            raise ValueError("failed assessment describes storage/integrity failure")
        return self


class EvidenceDocument(FrozenModel):
    schema_version: Literal[1] = 1
    artifact: ImmutableArtifact
    storage_state: Literal["PENDING", "READY", "FAILED", "CORRUPT"]
    assessment: Assessment
    integrity: Literal["not_checked", "valid", "failed"] = "not_checked"


@dataclass(frozen=True)
class ArtifactRead:
    evidence: EvidenceDocument
    content: bytes


class ArtifactStore(Protocol):
    def put(
        self,
        actor: Principal,
        run_id: str,
        workspace_id: str,
        sandbox_id: str,
        lease_token: str,
        idempotency_key: str,
        upload: ArtifactUpload,
        content: bytes,
    ) -> EvidenceDocument: ...
    def read(
        self, actor: Principal, run_id: str, artifact_id: str, lease_token: str | None = None
    ) -> ArtifactRead: ...
    def describe(
        self, actor: Principal, run_id: str, artifact_id: str, lease_token: str | None = None
    ) -> EvidenceDocument: ...
