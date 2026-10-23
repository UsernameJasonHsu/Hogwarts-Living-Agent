import hashlib
import json
from datetime import UTC, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from hogwarts.adapters import Adapters
from hogwarts.auth import Principal
from hogwarts.models import (
    Agent,
    ApprovalRequest,
    BudgetReservation,
    IdempotencyRecord,
    MergeRequest,
    Project,
    Proposal,
    PrototypeRun,
    WorkflowEvent,
    WorkspaceBudget,
    now,
    uid,
)
from hogwarts.schemas import ProposalCreate, State


class DomainError(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


GRAPH = {
    State.PROPOSED: {State.POLICY_CHECK, State.CANCELLED},
    State.POLICY_CHECK: {State.QUEUED, State.AWAITING_EXECUTION_APPROVAL},
    State.AWAITING_EXECUTION_APPROVAL: {State.PROPOSED, State.REJECTED, State.CANCELLED},
    State.QUEUED: {State.SANDBOX_READY, State.CANCELLED, State.FAILED},
    State.SANDBOX_READY: {State.IMPLEMENTING, State.FAILED, State.CANCELLED},
    State.IMPLEMENTING: {State.TESTING, State.FAILED, State.CANCELLED},
    State.TESTING: {State.REVIEWING, State.FAILED, State.CANCELLED},
    State.REVIEWING: {State.AWAITING_HUMAN_REVIEW, State.FAILED, State.CANCELLED},
    State.AWAITING_HUMAN_REVIEW: {State.ACCEPTED, State.REJECTED, State.DEFERRED, State.CANCELLED},
}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def require_human(actor):
    if actor.role != "human":
        raise DomainError(403, "human_required")


def emit(session, actor, aggregate_id, version, event_type, reason, key=None, **payload):
    session.add(
        WorkflowEvent(
            owner_id=actor.owner_id,
            aggregate_id=aggregate_id,
            aggregate_version=version,
            event_type=event_type,
            actor_id=actor.id,
            reason=reason,
            idempotency_key=key,
            payload=payload,
        )
    )


def command(factory, actor: Principal, key: str, operation: str, resource: str, body, function):
    """Unique key insertion serializes duplicate commands across processes, not Python locks."""
    request_hash = digest({"operation": operation, "resource": resource, "body": body})

    def replay(session):
        record = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.owner_id == actor.owner_id,
                IdempotencyRecord.actor_id == actor.id,
                IdempotencyRecord.key == key,
            )
        )
        if record:
            if record.request_hash != request_hash:
                raise DomainError(409, "idempotency_key_reused_with_different_request")
            return record.response
        return None

    try:
        try:
            with factory.begin() as session:
                existing = replay(session)
                if existing is not None:
                    return existing
                record = IdempotencyRecord(
                    owner_id=actor.owner_id, actor_id=actor.id, key=key, request_hash=request_hash
                )
                session.add(record)
                session.flush()
                result = function(session)
                record.response = result
                session.flush()
                return result
        except IntegrityError:
            # Recovery can also reject a conflicting body. Its DomainError must reach
            # the same audit handler as a rejection in the original transaction.
            with factory() as session:
                existing = replay(session)
                if existing is not None:
                    return existing
            raise DomainError(409, "unique_constraint_conflict") from None
    except DomainError as error:
        # The rejected business transaction has rolled back; preserve a separate denial event.
        with factory.begin() as session:
            emit(session, actor, resource, 0, "policy.denied", error.code, key, operation=operation)
        raise


def owned(session, model, identifier, actor):
    item = session.get(model, identifier)
    if not item or item.owner_id != actor.owner_id:
        raise DomainError(404, "not_found")
    return item


def proposal_for(session, identifier, actor):
    proposal = owned(session, Proposal, identifier, actor)
    if actor.role == "worker":
        agent = session.get(Agent, proposal.author_agent_id)
        if not agent or agent.owner_id != actor.owner_id or agent.key != actor.agent_key:
            raise DomainError(404, "not_found")
    return proposal


def proposal_view(proposal):
    return {
        "id": proposal.id,
        "project_id": proposal.project_id,
        "author_agent_id": proposal.author_agent_id,
        "status": proposal.status,
        "version": proposal.version,
        "document": proposal.document,
    }


def check_version(proposal, version):
    if proposal.version != version:
        raise DomainError(409, "stale_version")


def cas(
    session,
    proposal,
    actor,
    expected,
    *,
    status=None,
    document=None,
    event="proposal.changed",
    reason="command",
    key=None,
):
    check_version(proposal, expected)
    old_state = proposal.status
    if status is not None and status not in GRAPH.get(old_state, set()):
        raise DomainError(409, "invalid_transition")
    values = {"version": expected + 1}
    if status is not None:
        values["status"] = str(status)
    if document is not None:
        values["document"] = document
    changed = session.execute(
        update(Proposal)
        .where(
            Proposal.id == proposal.id,
            Proposal.owner_id == actor.owner_id,
            Proposal.version == expected,
            Proposal.status == old_state,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise DomainError(409, "stale_version")
    session.refresh(proposal)
    emit(
        session,
        actor,
        proposal.id,
        proposal.version,
        event,
        reason,
        key,
        from_state=old_state,
        to_state=proposal.status,
    )


def create_project(session, actor, data, key):
    require_human(actor)
    project = Project(owner_id=actor.owner_id, **data.model_dump())
    session.add(project)
    if session.get(WorkspaceBudget, actor.owner_id) is None:
        session.add(WorkspaceBudget(owner_id=actor.owner_id))
    session.flush()
    emit(
        session,
        actor,
        project.id,
        1,
        "project.granted",
        "explicit owner grant",
        key,
        allowed_paths=project.allowed_paths,
        preapproved=project.preapproved,
    )
    return {"id": project.id, **data.model_dump()}


def create_agent(session, actor, data, key):
    require_human(actor)
    agent = Agent(owner_id=actor.owner_id, **data.model_dump())
    session.add(agent)
    session.flush()
    emit(session, actor, agent.id, 1, "agent.created", "human registration", key)
    return {"id": agent.id, **data.model_dump()}


def validate_scope(project, data: ProposalCreate):
    # M1 handles lexical scope only. No filesystem is opened: symlink enforcement belongs to M2.
    for path in data.prototype.allowed_paths:
        if not any(
            path == grant or path.startswith(grant + "/") for grant in project.allowed_paths
        ):
            raise DomainError(403, "path_outside_owner_grant")


def validate_author(session, actor, data):
    project = owned(session, Project, data.project_id, actor)
    agent = owned(session, Agent, data.author_agent_id, actor)
    if actor.role == "worker" and actor.agent_key != agent.key:
        raise DomainError(403, "agent_identity_mismatch")
    validate_scope(project, data)
    return project, agent


def create_proposal(session, actor, data, key):
    validate_author(session, actor, data)
    proposal = Proposal(
        owner_id=actor.owner_id,
        project_id=data.project_id,
        author_agent_id=data.author_agent_id,
        document=data.model_dump(),
    )
    session.add(proposal)
    session.flush()
    emit(session, actor, proposal.id, 1, "proposal.created", "validated v1 command", key)
    return proposal_view(proposal)


def revise(session, actor, proposal_id, data, key):
    proposal = proposal_for(session, proposal_id, actor)
    if proposal.status not in {State.PROPOSED, State.AWAITING_EXECUTION_APPROVAL}:
        raise DomainError(409, "invalid_transition")
    if (data.proposal.project_id, data.proposal.author_agent_id) != (
        proposal.project_id,
        proposal.author_agent_id,
    ):
        raise DomainError(403, "proposal_identity_immutable")
    validate_author(session, actor, data.proposal)
    target = State.PROPOSED if proposal.status == State.AWAITING_EXECUTION_APPROVAL else None
    cas(
        session,
        proposal,
        actor,
        data.expected_version,
        status=target,
        document=data.proposal.model_dump(),
        event="proposal.revised",
        reason="revision",
        key=key,
    )
    return proposal_view(proposal)


def reservation(session, proposal, actor, run):
    amount = proposal.document["estimate_cents"]
    # Consistent lock order across all paths: workspace -> agent.
    changed = session.execute(
        update(WorkspaceBudget)
        .where(
            WorkspaceBudget.owner_id == actor.owner_id,
            WorkspaceBudget.reserved_cents + amount <= WorkspaceBudget.limit_cents,
            WorkspaceBudget.active_runs < 2,
        )
        .values(
            reserved_cents=WorkspaceBudget.reserved_cents + amount,
            active_runs=WorkspaceBudget.active_runs + 1,
        )
    )
    if changed.rowcount != 1:
        raise DomainError(409, "workspace_budget_or_concurrency_exhausted")
    changed = session.execute(
        update(Agent)
        .where(
            Agent.id == proposal.author_agent_id,
            Agent.owner_id == actor.owner_id,
            Agent.reserved_cents + amount <= Agent.budget_cents,
            Agent.active_runs < 1,
        )
        .values(reserved_cents=Agent.reserved_cents + amount, active_runs=Agent.active_runs + 1)
    )
    if changed.rowcount != 1:
        raise DomainError(409, "agent_budget_or_concurrency_exhausted")
    session.add(
        BudgetReservation(
            run_id=run.id,
            owner_id=actor.owner_id,
            agent_id=proposal.author_agent_id,
            estimate_cents=amount,
        )
    )


def release(session, run):
    budget = session.scalar(select(BudgetReservation).where(BudgetReservation.run_id == run.id))
    if budget is None or budget.status != "RESERVED":
        raise DomainError(409, "reservation_not_active")
    amount = budget.estimate_cents
    session.execute(
        update(WorkspaceBudget)
        .where(WorkspaceBudget.owner_id == budget.owner_id)
        .values(
            reserved_cents=WorkspaceBudget.reserved_cents - amount,
            active_runs=WorkspaceBudget.active_runs - 1,
        )
    )
    session.execute(
        update(Agent)
        .where(Agent.id == budget.agent_id)
        .values(reserved_cents=Agent.reserved_cents - amount, active_runs=Agent.active_runs - 1)
    )
    budget.status, budget.actual_cents = (
        "RELEASED",
        0,
    )  # Pure fixture execution has no billable calls.


def approval_matches(approval, proposal, project):
    return (
        approval.revision == proposal.version
        and approval.evidence_hash == digest(proposal.document)
        and approval.action == "sandbox.execute"
        and approval.actor_id == proposal.author_agent_id
        and approval.base_commit == proposal.document["prototype"]["base_commit"]
        and approval.target_branch == project.target_branch
        and utc(approval.expires_at) > now()
        and not approval.consumed
    )


def start(session, actor, proposal_id, data, key):
    proposal = proposal_for(session, proposal_id, actor)
    check_version(proposal, data.expected_version)
    if proposal.status != State.PROPOSED:
        raise DomainError(409, "invalid_transition")
    project = owned(session, Project, proposal.project_id, actor)
    validate_scope(project, ProposalCreate.model_validate(proposal.document))
    approved = None
    if not project.preapproved:
        for item in session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.proposal_id == proposal.id, ApprovalRequest.decision == "APPROVED"
            )
        ):
            if approval_matches(item, proposal, project):
                approved = item
                break
    cas(
        session,
        proposal,
        actor,
        proposal.version,
        status=State.POLICY_CHECK,
        event="policy.checked",
        reason="execution policy",
        key=key,
    )
    if not project.preapproved and approved is None:
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.AWAITING_EXECUTION_APPROVAL,
            event="approval.requested",
            reason="no preauthorization",
            key=key,
        )
        approval = ApprovalRequest(
            proposal_id=proposal.id,
            revision=proposal.version,
            evidence_hash=digest(proposal.document),
            actor_id=proposal.author_agent_id,
            base_commit=proposal.document["prototype"]["base_commit"],
            target_branch=project.target_branch,
            expires_at=now() + timedelta(hours=1),
        )
        session.add(approval)
        session.flush()
        return {**proposal_view(proposal), "approval_id": approval.id, "run_id": None}
    if approved:
        approved.consumed = True
    cas(
        session,
        proposal,
        actor,
        proposal.version,
        status=State.QUEUED,
        event="run.queued",
        reason="policy permitted fake execution",
        key=key,
    )
    run = PrototypeRun(proposal_id=proposal.id)
    session.add(run)
    session.flush()
    reservation(session, proposal, actor, run)
    return {**proposal_view(proposal), "run_id": run.id}


def decide_approval(session, actor, approval_id, data, key):
    require_human(actor)
    approval = session.get(ApprovalRequest, approval_id)
    if not approval:
        raise DomainError(404, "not_found")
    proposal = proposal_for(session, approval.proposal_id, actor)
    project = owned(session, Project, proposal.project_id, actor)
    check_version(proposal, data.expected_version)
    if (
        proposal.status != State.AWAITING_EXECUTION_APPROVAL
        or approval.decision != "PENDING"
        or not approval_matches(approval, proposal, project)
    ):
        raise DomainError(409, "stale_or_expired_approval")
    target = State.PROPOSED if data.decision == "APPROVED" else State.REJECTED
    cas(
        session,
        proposal,
        actor,
        proposal.version,
        status=target,
        event="approval.decided",
        reason=data.decision,
        key=key,
    )
    approval.decision, approval.approver_id = data.decision, actor.id
    approval.decided_at, approval.revision = now(), proposal.version
    return {**proposal_view(proposal), "approval_id": approval.id, "decision": approval.decision}


def claim(session, actor, run_id, data, key):
    if actor.role != "worker":
        raise DomainError(403, "worker_required")
    run = session.get(PrototypeRun, run_id)
    if not run:
        raise DomainError(404, "not_found")
    proposal = proposal_for(session, run.proposal_id, actor)
    if proposal.status != State.QUEUED:
        raise DomainError(409, "invalid_transition")
    cas(
        session,
        proposal,
        actor,
        data.expected_version,
        event="run.claimed",
        reason="atomic lease",
        key=key,
    )
    token, expiry = uid(), now() + timedelta(minutes=5)
    changed = session.execute(
        update(PrototypeRun)
        .where(
            PrototypeRun.id == run_id,
            PrototypeRun.status == "QUEUED",
            PrototypeRun.lease_token.is_(None),
        )
        .values(
            status="CLAIMED",
            worker_id=actor.id,
            lease_token=token,
            lease_expires_at=expiry,
            started_at=now(),
        )
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise DomainError(409, "already_claimed")
    return {
        "run_id": run_id,
        "lease_token": token,
        "lease_expires_at": expiry.isoformat(),
        "version": proposal.version,
        "status": "CLAIMED",
    }


def execute(session, actor, run_id, data, key, adapters: Adapters):
    if actor.role != "worker":
        raise DomainError(403, "worker_required")
    run = session.get(PrototypeRun, run_id)
    if not run:
        raise DomainError(404, "not_found")
    proposal = proposal_for(session, run.proposal_id, actor)
    if (
        run.status != "CLAIMED"
        or run.worker_id != actor.id
        or run.lease_token != data.lease_token
        or run.lease_expires_at is None
        or utc(run.lease_expires_at) <= now()
    ):
        raise DomainError(409, "invalid_or_expired_lease")
    project = owned(session, Project, proposal.project_id, actor)
    if proposal.status != State.QUEUED:
        raise DomainError(409, "invalid_transition")
    # Acquire the version lock before calling any adapter, without claiming readiness.
    cas(
        session,
        proposal,
        actor,
        data.expected_version,
        event="sandbox.requested",
        reason="allocating fake sandbox",
        key=key,
    )
    sandbox = None
    try:
        sandbox = adapters.sandbox.create(
            project.repo_ref, proposal.document["prototype"]["base_commit"], run.id
        )
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.SANDBOX_READY,
            event="sandbox.ready",
            reason="fake sandbox allocated",
            key=key,
        )
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.IMPLEMENTING,
            event="runtime.started",
            reason="FakeCodingRuntime",
            key=key,
        )
        patch = adapters.runtime.execute(sandbox, proposal.document, run.id)
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.TESTING,
            event="tests.started",
            reason="FakeTestRunner",
            key=key,
        )
        report = adapters.tests.run(sandbox)
        artifact = adapters.artifacts.put(patch)
        # Tool output is data: it cannot claim trusted verification or grant permissions.
        if (
            report.get("verified") is not False
            or report.get("command") is not None
            or report.get("exit_code") is not None
        ):
            raise ValueError("fake adapter attempted to forge trusted execution evidence")
        if report.get("fixture_status") != "PASS":
            raise ValueError("fake QA fixture failed")
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.REVIEWING,
            event="review.requested",
            reason="fixture evidence only",
            key=key,
        )
        run.evidence = {
            "mode": "fake",
            "verified": False,
            "artifact": artifact,
            "tests": report,
            "base_commit": sandbox.base_commit,
            "review": "No independent engineering review in M1",
        }
        adapters.sandbox.destroy(sandbox.id)
        sandbox = None
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.AWAITING_HUMAN_REVIEW,
            event="prototype.presented",
            reason="human review of fake fixture",
            key=key,
        )
        run.status = "COMPLETED"
    except Exception:
        # Do not persist exception strings: untrusted tools can include secrets in their errors.
        cas(
            session,
            proposal,
            actor,
            proposal.version,
            status=State.FAILED,
            event="run.failed",
            reason="adapter_failure",
            key=key,
        )
        run.status, run.evidence = "FAILED", {"mode": "fake", "error": "adapter_failure"}
    finally:
        if sandbox is not None:
            try:
                adapters.sandbox.destroy(sandbox.id)
            except Exception:
                run.evidence = {**(run.evidence or {}), "cleanup_failed": True}
                emit(
                    session,
                    actor,
                    proposal.id,
                    proposal.version,
                    "sandbox.cleanup_failed",
                    "fake adapter cleanup failure",
                    key,
                )
    run.ended_at = now()
    release(session, run)
    return {**proposal_view(proposal), "run_id": run.id, "evidence": run.evidence}


def cancel(session, actor, proposal_id, data, key):
    require_human(actor)
    proposal = proposal_for(session, proposal_id, actor)
    cas(
        session,
        proposal,
        actor,
        data.expected_version,
        status=State.CANCELLED,
        event="proposal.cancelled",
        reason="human cancellation",
        key=key,
    )
    run = session.scalar(select(PrototypeRun).where(PrototypeRun.proposal_id == proposal.id))
    if run and run.status in {"QUEUED", "CLAIMED"}:
        run.status, run.ended_at = "CANCELLED", now()
        release(session, run)
    return proposal_view(proposal)


def decision(session, actor, proposal_id, data, key):
    require_human(actor)
    proposal = proposal_for(session, proposal_id, actor)
    project = owned(session, Project, proposal.project_id, actor)
    cas(
        session,
        proposal,
        actor,
        data.expected_version,
        status=data.decision,
        event="prototype.decided",
        reason=data.decision,
        key=key,
    )
    approval = ApprovalRequest(
        proposal_id=proposal.id,
        revision=proposal.version,
        action="prototype.review",
        evidence_hash=digest(proposal.document),
        base_commit=proposal.document["prototype"]["base_commit"],
        target_branch=project.target_branch,
        actor_id=proposal.author_agent_id,
        expires_at=now(),
        decision=data.decision,
        approver_id=actor.id,
        decided_at=now(),
        consumed=True,
    )
    session.add(approval)
    session.flush()
    merge_id = None
    if data.decision == "ACCEPTED":
        intent = MergeRequest(
            proposal_id=proposal.id,
            base_commit=approval.base_commit,
            target_branch=project.target_branch,
            approval_id=approval.id,
        )
        session.add(intent)
        session.flush()
        merge_id = intent.id
        emit(
            session,
            actor,
            proposal.id,
            proposal.version,
            "merge.requested",
            "internal intent; separate approval and executor required",
            key,
            merge_request_id=merge_id,
        )
    return {**proposal_view(proposal), "merge_request_id": merge_id, "merged": False}
