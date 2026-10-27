"""Trusted operator fixture metadata, never configured from an Agent response."""

import os

from hogwarts.entrypoints.deployment import (
    DeploymentIdentity,
    DeploymentTrust,
    gateway_fingerprint,
    release_identity,
)

ORIGIN = "https://testserver"
TEST_KEY = b"rc1-inprocess-test-deployment-" + b"d" * 48


def identity(gateway):
    # Fake graph negative tests use a non-authorizing test pin; only real drivers
    # may supply the fingerprint accepted by production validate_gateway.
    from hogwarts.outputs.driver import OutputDockerDriver

    pin = (
        gateway_fingerprint(gateway)
        if type(gateway.orchestrator.controller.driver) is OutputDockerDriver
        else "c" * 64
    )
    return DeploymentIdentity(
        origin=ORIGIN,
        deployment_id="rc1-test",
        controller_image_id=os.getenv("RC1_CONTROLLER_IMAGE_ID", "sha256:" + "a" * 64),
        release_sha256=release_identity()["release_sha256"],
        gateway_sha256=pin,
        key=TEST_KEY,
    )


def trust(deployment):
    return DeploymentTrust(
        origin=deployment.origin,
        deployment_id=deployment.deployment_id,
        controller_image_id=deployment.controller_image_id,
        release_sha256=deployment.release_sha256,
        gateway_sha256=deployment.gateway_sha256,
        key=deployment.key,
    )


def inprocess_service(gateway):
    """Explicit non-deployment fake-driver facade; exposes no formal-worker routes."""
    from hogwarts.main import create_app

    return create_app(
        gateway.factory.kw["bind"],
        formal_gateway=gateway,
        enforce_fake_adapters=True,
        execution_mode="formal-inprocess-test",
    )
