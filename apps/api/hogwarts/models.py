from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from hogwarts.db import Base


def uid():
    return str(uuid4())


def now():
    return datetime.now(UTC)


class Agent(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    key: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(100), default="engineer")
    budget_cents: Mapped[int] = mapped_column(default=100)
    reserved_cents: Mapped[int] = mapped_column(default=0)
    active_runs: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        UniqueConstraint("owner_id", "key"),
        CheckConstraint("reserved_cents >= 0 AND reserved_cents <= budget_cents"),
        CheckConstraint("active_runs >= 0 AND active_runs <= 1"),
    )


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    repo_ref: Mapped[str] = mapped_column(String(500))
    allowed_paths: Mapped[list] = mapped_column(JSON)
    preapproved: Mapped[bool] = mapped_column(default=False)
    target_branch: Mapped[str] = mapped_column(String(100), default="main")


class WorkspaceBudget(Base):
    __tablename__ = "workspace_budgets"
    owner_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    limit_cents: Mapped[int] = mapped_column(default=500)
    reserved_cents: Mapped[int] = mapped_column(default=0)
    active_runs: Mapped[int] = mapped_column(default=0)
    __table_args__ = (
        CheckConstraint("reserved_cents >= 0 AND reserved_cents <= limit_cents"),
        CheckConstraint("active_runs >= 0 AND active_runs <= 2"),
    )


class Proposal(Base):
    __tablename__ = "proposals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    author_agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    status: Mapped[str] = mapped_column(String(50), default="PROPOSED", index=True)
    version: Mapped[int] = mapped_column(default=1)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PrototypeRun(Base):
    __tablename__ = "prototype_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    status: Mapped[str] = mapped_column(String(50), default="QUEUED", index=True)
    attempt_no: Mapped[int] = mapped_column(default=1)
    worker_id: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence: Mapped[dict | None] = mapped_column(JSON)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), index=True)
    action: Mapped[str] = mapped_column(String(50), default="sandbox.execute")
    revision: Mapped[int]
    evidence_hash: Mapped[str] = mapped_column(String(64))
    base_commit: Mapped[str] = mapped_column(String(40))
    target_branch: Mapped[str] = mapped_column(String(100))
    actor_id: Mapped[str] = mapped_column(String(36))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str] = mapped_column(String(20), default="PENDING")
    approver_id: Mapped[str | None] = mapped_column(String(100))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(default=False)


class BudgetReservation(Base):
    __tablename__ = "budget_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("prototype_runs.id"), unique=True)
    owner_id: Mapped[str] = mapped_column(String(100))
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    estimate_cents: Mapped[int]
    actual_cents: Mapped[int | None]
    status: Mapped[str] = mapped_column(String(20), default="RESERVED")
    __table_args__ = (CheckConstraint("estimate_cents > 0"),)


class WorkflowEvent(Base):
    """Append-only application log, also the durable undelivered outbox."""

    __tablename__ = "workflow_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_id: Mapped[str] = mapped_column(String(100), index=True)
    aggregate_version: Mapped[int]
    event_type: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(100), default="governance")
    schema_version: Mapped[int] = mapped_column(default=1)
    actor_id: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(200))
    correlation_id: Mapped[str] = mapped_column(String(36), default=uid)
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dispatched: Mapped[bool] = mapped_column(default=False)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(100))
    actor_id: Mapped[str] = mapped_column(String(100))
    key: Mapped[str] = mapped_column(String(100))
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("owner_id", "actor_id", "key"),)


class MergeRequest(Base):
    """Internal intent only: no SCM adapter or merge executor exists in M1."""

    __tablename__ = "merge_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    proposal_id: Mapped[str] = mapped_column(ForeignKey("proposals.id"), unique=True)
    base_commit: Mapped[str] = mapped_column(String(40))
    target_branch: Mapped[str] = mapped_column(String(100))
    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"))
    status: Mapped[str] = mapped_column(String(50), default="PENDING_SEPARATE_APPROVAL")
