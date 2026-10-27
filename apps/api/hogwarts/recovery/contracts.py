from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from hogwarts.auth import Principal
from hogwarts.service import DomainError

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
Classification = Literal["SAFE_RESUME", "FINALIZE", "MANUAL_REQUIRED"]
Action = Literal[
    "NONE", "IMPORT", "PATCH", "QA", "VERIFICATION", "SETTLE_BUDGET", "CANCEL_METADATA"
]


def deny(code, status=409):
    raise DomainError(status, code)


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Decision(Frozen):
    classification: Classification
    action: Action = "NONE"
    reason: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,100}$")]
    target_id: Identity | None = None


class Observation(Frozen):
    run_id: Identity
    project_id: Identity
    generation: int
    lease_version: int
    fingerprint: Sha
    stalled: bool
    decision: Decision
    artifact_hashes: dict[str, Sha]
    proof_sha256: Sha | None = None
    component_hashes: dict[str, Sha] = {}


class RecoveryJournal(Frozen):
    schema_version: Literal[1] = 1
    recovery_id: Identity
    owner_id: str
    actor_id: str
    observation: Observation
    created_at: str
    state: Literal["PLANNED", "EXECUTING", "COMPLETE", "MANUAL_REQUIRED", "UNKNOWN"] = "PLANNED"
    version: Annotated[int, Field(ge=1)] = 1
    error_code: str | None = None
    result: Observation | None = None


class HumanIntervention(Frozen):
    schema_version: Literal[1] = 1
    intervention_id: Identity
    owner_id: str
    run_id: Identity
    recovery_id: Identity
    generation: int
    observation_sha256: Sha
    reason: str
    status: Literal["OPEN", "ACKNOWLEDGED"] = "OPEN"
    version: Annotated[int, Field(ge=1)] = 1


class CrashRecovery(Protocol):
    def recover(
        self,
        actor: Principal,
        run_id: str,
        key: str,
        lease_token: str | None = None,
        *,
        dry_run: bool = True,
    ) -> RecoveryJournal: ...
