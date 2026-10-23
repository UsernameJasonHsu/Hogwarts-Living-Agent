from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Text = Annotated[str, Field(min_length=1, max_length=5000)]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]
Commit = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


def safe_path(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or "\\" in value
        or ":" in value
        or any(part in {"", ".", "..", ".git"} for part in parts)
        or any(ord(char) < 32 for char in value)
        or any(char in value for char in "*?[]%")
        or str(PurePosixPath(value)) != value
    ):
        raise ValueError("use a canonical repository-relative path without globbing or traversal")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectCreate(StrictModel):
    repo_ref: Annotated[
        str, Field(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
    ]
    allowed_paths: Annotated[list[Text], Field(min_length=1, max_length=100)]
    preapproved: bool = False
    target_branch: Annotated[str, Field(pattern=r"^[A-Za-z0-9_/-]{1,100}$")] = "main"

    @field_validator("allowed_paths")
    @classmethod
    def paths(cls, values):
        return [safe_path(value) for value in values]


class AgentCreate(StrictModel):
    key: Identifier
    display_name: Identifier
    budget_cents: Annotated[int, Field(ge=0, le=100)] = 100


class Evidence(StrictModel):
    type: Text
    source_ref: Text


class Problem(StrictModel):
    description: Text
    evidence: list[Evidence]


class PrototypeScope(StrictModel):
    base_commit: Commit
    allowed_paths: Annotated[list[Text], Field(min_length=1, max_length=100)]
    max_runtime_seconds: Annotated[int, Field(ge=1, le=3600)]

    @field_validator("allowed_paths")
    @classmethod
    def paths(cls, values):
        return [safe_path(value) for value in values]


class ProposalCreate(StrictModel):
    project_id: Identifier
    author_agent_id: Identifier
    origin: Literal["assigned", "reactive", "self_proposed"]
    title: Text
    problem: Problem
    prototype: PrototypeScope
    acceptance_criteria: Annotated[list[Text], Field(min_length=1)]
    estimate_cents: Annotated[int, Field(ge=1, le=500)] = 10


class VersionCommand(StrictModel):
    expected_version: Annotated[int, Field(ge=1)]


class RevisionCommand(VersionCommand):
    proposal: ProposalCreate


class State(StrEnum):
    PROPOSED = "PROPOSED"
    POLICY_CHECK = "POLICY_CHECK"
    AWAITING_EXECUTION_APPROVAL = "AWAITING_EXECUTION_APPROVAL"
    QUEUED = "QUEUED"
    SANDBOX_READY = "SANDBOX_READY"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    AWAITING_HUMAN_REVIEW = "AWAITING_HUMAN_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class HumanDecision(VersionCommand):
    decision: Literal["ACCEPTED", "REJECTED", "DEFERRED"]


class ApprovalDecision(VersionCommand):
    decision: Literal["APPROVED", "REJECTED"]


class ExecuteCommand(VersionCommand):
    lease_token: Identifier
