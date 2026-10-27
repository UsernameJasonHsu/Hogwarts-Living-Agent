from typing import Annotated

from fastapi import Depends, FastAPI, Header
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from hogwarts import service
from hogwarts.adapters import fake_adapters
from hogwarts.auth import Principal, authenticate
from hogwarts.db import database_url, make_engine, sessions
from hogwarts.models import (
    Agent,
    ApprovalRequest,
    MergeRequest,
    Proposal,
    PrototypeRun,
    WorkflowEvent,
)
from hogwarts.schemas import (
    AgentCreate,
    ApprovalDecision,
    ExecuteCommand,
    HumanDecision,
    ProjectCreate,
    ProposalCreate,
    RevisionCommand,
    VersionCommand,
)

Actor = Annotated[Principal, Depends(authenticate)]
Key = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$"),
]


def create_app(
    engine=None,
    adapters=None,
    *,
    formal_gateway=None,
    enforce_fake_adapters=None,
    execution_enabled=True,
    execution_mode="compatibility-fake",
):
    """Compatibility construction; approved deployment uses formal bootstrap."""
    app = FastAPI(title="Hogwarts Governance", version="0.1.0")
    app.state.engine = engine if engine is not None else make_engine(database_url())
    factory = sessions(app.state.engine)
    app.state.adapters = adapters if adapters is not None else fake_adapters()
    app.state.formal_gateway = formal_gateway
    strict_fake = engine is None if enforce_fake_adapters is None else enforce_fake_adapters
    if strict_fake:
        from hogwarts.entrypoints.bootstrap import sealed_fake_check

        check_fake = sealed_fake_check()

    @app.exception_handler(service.DomainError)
    async def domain_error(_, error):
        return JSONResponse(status_code=error.status, content={"detail": error.code})

    def mutate(actor, key, operation, resource, data, function):
        body = data.model_dump(mode="json") if data is not None else {}
        return service.command(factory, actor, key, operation, resource, body, function)

    @app.get("/health")
    def health():
        with app.state.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "milestone": "M0/M1", "execution_mode": execution_mode}

    @app.post("/api/v1/projects", tags=["grants"])
    def create_project(data: ProjectCreate, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "project.create",
            "projects",
            data,
            lambda s: service.create_project(s, actor, data, key),
        )

    @app.post("/api/v1/agents", tags=["agents"])
    def create_agent(data: AgentCreate, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "agent.create",
            "agents",
            data,
            lambda s: service.create_agent(s, actor, data, key),
        )

    @app.get("/api/v1/agents", tags=["agents"])
    def list_agents(actor: Actor):
        with factory() as session:
            query = select(Agent).where(Agent.owner_id == actor.owner_id)
            if actor.role == "worker":
                query = query.where(Agent.key == actor.agent_key)
            return [
                {
                    "id": a.id,
                    "key": a.key,
                    "display_name": a.display_name,
                    "budget_cents": a.budget_cents,
                }
                for a in session.scalars(query)
            ]

    @app.post("/api/v1/prototypes", tags=["prototypes"])
    def create_proposal(data: ProposalCreate, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "proposal.create",
            "proposals",
            data,
            lambda s: service.create_proposal(s, actor, data, key),
        )

    @app.get("/api/v1/prototypes", tags=["prototypes"])
    def list_proposals(actor: Actor, limit: int = 50, offset: int = 0):
        with factory() as session:
            query = select(Proposal).where(Proposal.owner_id == actor.owner_id)
            if actor.role == "worker":
                query = query.join(Agent, Agent.id == Proposal.author_agent_id).where(
                    Agent.key == actor.agent_key, Agent.owner_id == actor.owner_id
                )
            query = query.order_by(Proposal.created_at, Proposal.id).limit(max(1, min(limit, 100)))
            return [service.proposal_view(p) for p in session.scalars(query.offset(max(offset, 0)))]

    @app.get("/api/v1/prototypes/{proposal_id}", tags=["prototypes"])
    def detail(proposal_id: str, actor: Actor):
        with factory() as session:
            return service.proposal_view(service.proposal_for(session, proposal_id, actor))

    @app.patch("/api/v1/prototypes/{proposal_id}", tags=["prototypes"])
    def revise(proposal_id: str, data: RevisionCommand, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "proposal.revise",
            proposal_id,
            data,
            lambda s: service.revise(s, actor, proposal_id, data, key),
        )

    @app.post("/api/v1/prototypes/{proposal_id}/start", tags=["prototypes"])
    def start(proposal_id: str, data: VersionCommand, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "proposal.start",
            proposal_id,
            data,
            lambda s: service.start(s, actor, proposal_id, data, key),
        )

    @app.post("/api/v1/prototypes/{proposal_id}/cancel", tags=["prototypes"])
    def cancel(proposal_id: str, data: VersionCommand, actor: Actor, key: Key):
        result = mutate(
            actor,
            key,
            "proposal.cancel",
            proposal_id,
            data,
            lambda s: service.cancel(
                s, actor, proposal_id, data, key, preserve_budget=formal_gateway is not None
            ),
        )
        if formal_gateway:
            result = {**result, "stop_state": formal_gateway.stop_cancelled(actor, proposal_id)}
        return result

    @app.post("/api/v1/prototypes/{proposal_id}/decision", tags=["human approval"])
    def decision(proposal_id: str, data: HumanDecision, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "proposal.decision",
            proposal_id,
            data,
            lambda s: service.decision(s, actor, proposal_id, data, key),
        )

    @app.get("/api/v1/prototypes/{proposal_id}/approvals", tags=["human approval"])
    def approvals(proposal_id: str, actor: Actor):
        with factory() as session:
            service.proposal_for(session, proposal_id, actor)
            return [
                {
                    "id": a.id,
                    "action": a.action,
                    "revision": a.revision,
                    "evidence_hash": a.evidence_hash,
                    "base_commit": a.base_commit,
                    "target_branch": a.target_branch,
                    "actor_id": a.actor_id,
                    "decision": a.decision,
                    "expires_at": service.utc(a.expires_at).isoformat(),
                    "approver_id": a.approver_id,
                    "consumed": a.consumed,
                }
                for a in session.scalars(
                    select(ApprovalRequest).where(ApprovalRequest.proposal_id == proposal_id)
                )
            ]

    @app.post("/api/v1/approvals/{approval_id}/decision", tags=["human approval"])
    def decide_approval(approval_id: str, data: ApprovalDecision, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "approval.decide",
            approval_id,
            data,
            lambda s: service.decide_approval(s, actor, approval_id, data, key),
        )

    @app.post("/api/v1/runs/{run_id}/claim", tags=["fake worker"])
    def claim(run_id: str, data: VersionCommand, actor: Actor, key: Key):
        return mutate(
            actor,
            key,
            "run.claim",
            run_id,
            data,
            lambda s: service.claim(s, actor, run_id, data, key),
        )

    @app.post("/api/v1/runs/{run_id}/execute", tags=["fake worker"])
    def execute(run_id: str, data: ExecuteCommand, actor: Actor, key: Key):
        if formal_gateway:
            return formal_gateway.execute(
                actor, run_id, data.expected_version, data.lease_token, key
            )

        def dispatch(session):
            if not execution_enabled:
                raise service.DomainError(503, "formal_runtime_not_configured")
            if strict_fake:
                check_fake(app.state.adapters)
            return service.execute(session, actor, run_id, data, key, app.state.adapters)

        return mutate(
            actor,
            key,
            "run.execute",
            run_id,
            data,
            dispatch,
        )

    @app.get("/api/v1/prototypes/{proposal_id}/runs", tags=["fake worker"])
    def runs(proposal_id: str, actor: Actor):
        with factory() as session:
            service.proposal_for(session, proposal_id, actor)
            return [
                {"id": r.id, "status": r.status, "attempt_no": r.attempt_no, "evidence": r.evidence}
                for r in session.scalars(
                    select(PrototypeRun).where(PrototypeRun.proposal_id == proposal_id)
                )
            ]

    @app.get("/api/v1/prototypes/{proposal_id}/events", tags=["audit"])
    def events(proposal_id: str, actor: Actor):
        with factory() as session:
            service.proposal_for(session, proposal_id, actor)
            return [
                {
                    "event_id": e.id,
                    "event_type": e.event_type,
                    "schema_version": e.schema_version,
                    "occurred_at": service.utc(e.occurred_at).isoformat(),
                    "source": e.source,
                    "correlation_id": e.correlation_id,
                    "idempotency_key": e.idempotency_key,
                    "payload": {
                        **e.payload,
                        "actor_id": e.actor_id,
                        "reason": e.reason,
                        "aggregate_version": e.aggregate_version,
                    },
                }
                for e in session.scalars(
                    select(WorkflowEvent)
                    .where(
                        WorkflowEvent.owner_id == actor.owner_id,
                        WorkflowEvent.aggregate_id == proposal_id,
                    )
                    .order_by(WorkflowEvent.occurred_at, WorkflowEvent.id)
                )
            ]

    @app.get("/api/v1/merge-requests", tags=["human approval"])
    def merge_requests(actor: Actor):
        service.require_human(actor)
        with factory() as session:
            return [
                {
                    "id": m.id,
                    "proposal_id": m.proposal_id,
                    "status": m.status,
                    "base_commit": m.base_commit,
                    "target_branch": m.target_branch,
                }
                for m in session.scalars(
                    select(MergeRequest).join(Proposal).where(Proposal.owner_id == actor.owner_id)
                )
            ]

    @app.post("/api/v1/prototypes/{proposal_id}/merge", tags=["forbidden actions"])
    @app.post("/api/v1/prototypes/{proposal_id}/deploy", tags=["forbidden actions"])
    def forbidden(proposal_id: str, data: VersionCommand, actor: Actor, key: Key):
        def deny(session):
            service.proposal_for(session, proposal_id, actor)
            raise service.DomainError(403, "merge_and_deploy_executor_absent_in_M1")

        return mutate(actor, key, "restricted.execute", proposal_id, data, deny)

    return app
