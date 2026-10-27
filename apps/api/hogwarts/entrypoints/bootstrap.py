"""Canonical deployment factory: explicit mode, no generated runtime import path."""

import os

from hogwarts.adapters import (
    FakeArtifactStore,
    FakeCodingRuntime,
    FakeSandboxManager,
    FakeTestRunner,
)
from hogwarts.reconciliation.contracts import deny


def sealed_fake_check():
    definitions = {
        "runtime": (FakeCodingRuntime, ("execute",)),
        "sandbox": (FakeSandboxManager, ("create", "destroy")),
        "artifacts": (FakeArtifactStore, ("put",)),
        "tests": (FakeTestRunner, ("run",)),
    }
    methods = {
        (name, method): getattr(cls, method)
        for name, (cls, names) in definitions.items()
        for method in names
    }

    def check(bundle):
        for name, (cls, names) in definitions.items():
            obj = getattr(bundle, name, None)
            if type(obj) is not cls or object.__getattribute__(obj, "__dict__"):
                deny("formal_nonfake_adapter_forbidden", 403)
            for method in names:
                if getattr(getattr(obj, method), "__func__", None) is not methods[name, method]:
                    deny("formal_nonfake_adapter_forbidden", 403)

    return check


_CANONICAL_FACTORY = object()


def create_formal_app(engine=None, *, gateway=None, mode=None, deployment=None):
    from hogwarts.main import create_app

    selected = mode or os.getenv("HOGWARTS_EXECUTION_MODE", "disabled")
    if selected not in {"disabled", "fake", "operational"}:
        deny("formal_invalid_execution_mode", 403)
    if (selected == "operational") != (gateway is not None):
        deny("formal_runtime_not_configured", 503)
    if selected != "operational" and deployment is not None:
        deny("formal_deployment_mode_mismatch", 403)
    if gateway is not None:
        from hogwarts.entrypoints.gateway import FormalExecutionGateway

        if type(gateway) is not FormalExecutionGateway:
            deny("formal_invalid_gateway", 403)
        if engine is None:
            engine = gateway.factory.kw["bind"]
        if engine is not gateway.factory.kw["bind"]:
            deny("formal_database_binding_mismatch", 403)
        from hogwarts.entrypoints.deployment import DeploymentIdentity

        if type(deployment) is not DeploymentIdentity:
            deny("formal_deployment_identity_required", 503)
        deployment.validate_gateway(gateway)
    app = create_app(
        engine,
        formal_gateway=gateway,
        enforce_fake_adapters=True,
        execution_enabled=selected != "disabled",
        execution_mode="formal-" + selected,
    )
    app.state._canonical_factory = _CANONICAL_FACTORY
    app.state._canonical_mode = selected
    if gateway is not None:
        from hogwarts.entrypoints.worker_api import install_worker_routes

        install_worker_routes(app, gateway, deployment)
    return app
