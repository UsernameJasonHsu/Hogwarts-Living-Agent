"""Canonical-only worker routes. HMAC binds deployment; M1 still grants actions."""

from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from hogwarts import service
from hogwarts.auth import Principal, authenticate
from hogwarts.entrypoints.deployment import PREFIX, DeploymentError, DeploymentIdentity
from hogwarts.models import PrototypeRun, WorkflowEvent
from hogwarts.schemas import ExecuteCommand, VersionCommand

Actor = Annotated[Principal, Depends(authenticate)]
Key = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$"),
]


def install_worker_routes(app, gateway, identity):
    if type(identity) is not DeploymentIdentity:
        raise DeploymentError("deployment_identity_required")
    identity.validate_gateway(gateway)
    router = APIRouter(prefix=PREFIX, tags=["trusted operational worker"])

    async def checked(request: Request, actor: Actor):
        if actor.role != "worker":
            raise service.DomainError(403, "worker_required")
        try:
            identity.validate_gateway(gateway)
            body = await request.json() if request.method == "POST" else None
            nonce = identity.verify_request(
                request.method, request.url.path, body, request.headers, str(request.base_url)
            )
            return body, nonce
        except (DeploymentError, ValueError):
            try:
                with gateway.factory.begin() as s:
                    s.add(
                        WorkflowEvent(
                            owner_id=actor.owner_id,
                            aggregate_id=str(
                                uuid5(NAMESPACE_URL, "formal-worker:" + identity.deployment_id)
                            ),
                            aggregate_version=1,
                            event_type="formal_worker.deployment_denied",
                            source="formal_worker",
                            actor_id=actor.id,
                            reason="deployment_rejected",
                            payload={"deployment_sha256": identity.fingerprint},
                        )
                    )
            except SQLAlchemyError:
                raise service.DomainError(
                    503, "formal_worker_deployment_audit_unavailable"
                ) from None
            raise service.DomainError(403, "formal_worker_deployment_rejected") from None

    Binding = Annotated[tuple, Depends(checked)]

    def result(request, body, nonce, data):
        identity.validate_gateway(gateway)
        return identity.response(request.method, request.url.path, body, nonce, data)

    @router.post("/deployment/challenge")
    def challenge(request: Request, binding: Binding):
        body, nonce = binding
        if body != {"challenge": "deployment-v1"}:
            raise service.DomainError(403, "formal_worker_challenge_rejected")
        return result(request, body, nonce, {"descriptor": identity.descriptor()})

    @router.get("/prototypes/{proposal_id}/context")
    def context(proposal_id: str, request: Request, actor: Actor, binding: Binding):
        body, nonce = binding
        with gateway.factory() as s:
            p = service.proposal_for(s, proposal_id, actor)
            runs = [
                {"id": r.id, "status": r.status}
                for r in s.scalars(select(PrototypeRun).where(PrototypeRun.proposal_id == p.id))
            ]
            data = {"proposal": service.proposal_view(p), "runs": runs}
        return result(request, body, nonce, data)

    @router.post("/runs/{run_id}/claim")
    def claim(
        run_id: str,
        data: VersionCommand,
        request: Request,
        actor: Actor,
        key: Key,
        binding: Binding,
    ):
        body, nonce = binding
        value = service.command(
            gateway.factory,
            actor,
            key,
            "run.claim",
            run_id,
            data.model_dump(mode="json"),
            lambda s: service.claim(s, actor, run_id, data, key),
        )
        return result(request, body, nonce, value)

    @router.post("/runs/{run_id}/execute")
    def execute(
        run_id: str,
        data: ExecuteCommand,
        request: Request,
        actor: Actor,
        key: Key,
        binding: Binding,
    ):
        body, nonce = binding
        value = gateway.execute(actor, run_id, data.expected_version, data.lease_token, key)
        return result(request, body, nonce, value)

    app.include_router(router)
