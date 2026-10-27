from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from hogwarts.auth import Principal
from hogwarts.service import DomainError

Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
Disposition = Literal["KEEP", "CLEANUP_ELIGIBLE", "QUARANTINE"]


def deny(code, status=409):
    raise DomainError(status, code)


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Resource(Frozen):
    key: Annotated[str, Field(min_length=1, max_length=150)]
    kind: Literal["container", "volume", "worktree", "evidence", "unknown"]
    identifier: Annotated[str, Field(min_length=1, max_length=200)]
    fingerprint: Sha
    disposition: Disposition
    reason: Annotated[str, Field(pattern=r"^[a-z0-9_]{1,100}$")]


class CleanupJournal(Frozen):
    schema_version: Literal[1] = 1
    plan_id: Identity
    run_id: Identity
    owner_id: str
    actor_id: str
    project_id: Identity
    generation: Annotated[int, Field(ge=0)]
    lease_version: Annotated[int, Field(ge=0)]
    binding_sha256: Sha
    resources: tuple[Resource, ...]
    state: Literal["PLANNED", "EXECUTING", "COMPLETE", "QUARANTINED", "UNKNOWN"] = "PLANNED"
    version: Annotated[int, Field(ge=1)] = 1
    created_at: str
    completed: tuple[str, ...] = ()
    error_code: str | None = None


class Reconciler(Protocol):
    def plan(self, actor: Principal, run_id: str, key: str) -> CleanupJournal: ...
    def execute(
        self, actor: Principal, run_id: str, plan_id: str, expected_version: int
    ) -> CleanupJournal: ...
