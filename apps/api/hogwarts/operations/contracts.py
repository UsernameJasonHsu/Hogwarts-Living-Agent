"""Operator-owned versioned operational contracts; no commands or caller paths."""

from typing import Annotated, Literal

from pydantic import Field

from hogwarts.reconciliation.contracts import Frozen, Identity, Sha

Count = Annotated[int, Field(ge=0, le=2**50)]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")]


class Capacity(Frozen):
    slots: Count = 0
    cpu_millis: Count = 0
    memory_bytes: Count = 0
    pids: Count = 0
    storage_bytes: Count = 0


class QuotaProfile(Frozen):
    name: Name
    demand: Capacity
    runtime_profile: Name
    qa_profile: Name


class QuotaReservation(Frozen):
    reservation_id: Identity
    run_id: Identity
    project_id: Identity
    actor_id: str
    generation: int
    profile: Name
    profile_sha256: Sha
    request_key: str
    demand: Capacity
    state: Literal["RESERVED", "IN_USE", "COMPLETED", "UNKNOWN", "RELEASED"] = "RESERVED"
    result: dict | None = None


class QuotaLedger(Frozen):
    schema_version: Literal[1] = 1
    owner_id: str
    limits_sha256: Sha
    version: Annotated[int, Field(ge=1)] = 1
    reservations: tuple[QuotaReservation, ...] = ()


class VersionedRetentionPolicy(Frozen):
    schema_version: Literal[1] = 1
    name: Name
    version: Annotated[int, Field(ge=1)] = 1
    ephemeral_ttl_seconds: Annotated[int, Field(ge=0, le=31536000)] = 86400
    approval_ttl_seconds: Annotated[int, Field(ge=1, le=3600)] = 300


class EvidenceHold(Frozen):
    schema_version: Literal[1] = 1
    run_id: Identity
    owner_id: str
    active: bool
    reason: Literal["review", "incident", "legal", "operator"]
    version: Annotated[int, Field(ge=1)] = 1


class GCJournal(Frozen):
    schema_version: Literal[1] = 1
    plan_id: Identity
    run_id: Identity
    owner_id: str
    actor_id: str
    generation: int
    lease_version: int
    cleanup_id: Identity
    cleanup_version: int
    policy_sha256: Sha
    references_sha256: Sha
    resources_sha256: Sha
    created_at: str
    eligible: bool
    reasons: tuple[str, ...]
    state: Literal["DRY_RUN", "APPROVED", "EXECUTING", "COMPLETE", "BLOCKED", "UNKNOWN"] = "DRY_RUN"
    version: Annotated[int, Field(ge=1)] = 1
    approved_by: str | None = None
    approval_expires_at: str | None = None
    error_code: str | None = None


class StorageUsage(Frozen):
    schema_version: Literal[1] = 1
    logical_bytes: Count
    declared_artifact_bytes: Count
    unmaterialized_artifact_bytes: Count = 0
    incomplete_artifact_count: Count = 0
    artifact_count: Count
    file_count: Count
    free_bytes_min: Count
    fingerprint: Sha
