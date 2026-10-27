"""RC-FIX-02 contracts; markers have no external effects."""

import importlib.util
from pathlib import Path

import pytest
from conftest import headers, post
from fastapi.testclient import TestClient
from formal_identity import ORIGIN, identity, trust
from hogwarts.adapters import FakeCodingRuntime
from hogwarts.entrypoints.bootstrap import create_formal_app
from hogwarts.entrypoints.deployment import DeploymentError, DeploymentTrust
from hogwarts.models import PrototypeRun
from hogwarts.service import DomainError
from rc1_support import queued_workspace as queued_workspace
from test_formal import gateway
from test_verification import verification as verification_factory
from test_workspaces import workspace as workspace


def fake_worker():
    spec = importlib.util.spec_from_file_location(
        "rc1_fake_worker", Path("workers/engineering/fake_worker.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def worker():
    spec = importlib.util.spec_from_file_location(
        "rc1_worker", Path("workers/engineering/worker.py")
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_rc1_worker_missing_trust_denies_before_http():
    class Never:
        def __getattr__(self, name):
            pytest.fail("HTTP attempted before trust validation")

    with pytest.raises(DeploymentError):
        worker().run_once(Never(), "missing")


@pytest.mark.parametrize("bad", ["key", "origin", "deployment", "image", "release", "gateway"])
def test_rc1_deployment_trust_requires_explicit_valid_fields(bad):
    values = {
        "origin": "https://testserver",
        "deployment_id": "rc1-test",
        "controller_image_id": "sha256:" + "a" * 64,
        "release_sha256": "b" * 64,
        "gateway_sha256": "c" * 64,
        "key": b"d" * 48,
    }
    values[
        {
            "key": "key",
            "origin": "origin",
            "deployment": "deployment_id",
            "image": "controller_image_id",
            "release": "release_sha256",
            "gateway": "gateway_sha256",
        }[bad]
    ] = b"short" if bad == "key" else "INVALID!"
    with pytest.raises(DeploymentError):
        DeploymentTrust(**values)


def test_rc1_legacy_injected_server_is_not_called(api, seed):
    calls = []

    class Marker(FakeCodingRuntime):
        def execute(self, *args):
            calls.append(True)
            return super().execute(*args)

    api.app.state.adapters.runtime = Marker()
    p = post(api, "prototypes", seed).json()
    post(api, f"prototypes/{p['id']}/start", {"expected_version": 1})
    trust = DeploymentTrust(
        origin="https://testserver",
        deployment_id="rc1-test",
        controller_image_id="sha256:" + "a" * 64,
        release_sha256="b" * 64,
        gateway_sha256="c" * 64,
        key=b"d" * 48,
    )
    with TestClient(api.app, base_url="https://testserver", headers=headers("worker")) as client:
        with pytest.raises(DeploymentError, match="deployment_http_rejected"):
            worker().run_once(client, p["id"], trust=trust)
    assert not calls


def test_rc1_fake_graph_cannot_be_production_deployment(queued_workspace):
    w = queued_workspace
    v = verification_factory.__wrapped__(w)
    g = gateway(v)
    d = identity(g)
    with pytest.raises(DeploymentError, match="deployment_linux_runtime_required"):
        create_formal_app(w["factory"].kw["bind"], gateway=g, mode="operational", deployment=d)
    assert not v[2].calls and not v[3].calls


def test_rc1_inprocess_service_has_no_production_worker_route(queued_workspace):
    w = queued_workspace
    v = verification_factory.__wrapped__(w)
    g = gateway(v)
    d = identity(g)
    t = trust(d)
    from formal_identity import inprocess_service

    with TestClient(inprocess_service(g), base_url=ORIGIN, headers=headers("worker")) as c:
        with pytest.raises(DeploymentError):
            worker().run_once(c, w["proposal"], trust=t)
    with w["factory"]() as s:
        assert s.get(PrototypeRun, w["run"]).lease_token is None
    assert not v[2].calls and not v[3].calls


def test_rc1_missing_server_identity_fails_closed(queued_workspace):
    w = queued_workspace
    g = gateway(verification_factory.__wrapped__(w))
    with pytest.raises(DomainError, match="formal_deployment_identity_required"):
        create_formal_app(w["factory"].kw["bind"], gateway=g, mode="operational")


def test_rc1_fake_test_mode_is_separate(api, seed, engine):
    p = post(api, "prototypes", seed).json()
    post(api, f"prototypes/{p['id']}/start", {"expected_version": 1})
    app = create_formal_app(engine, mode="fake")
    with TestClient(app, base_url=ORIGIN, headers=headers("worker")) as c:
        t = DeploymentTrust(
            origin=ORIGIN,
            deployment_id="rc1-test",
            controller_image_id="sha256:" + "a" * 64,
            release_sha256="b" * 64,
            gateway_sha256="c" * 64,
            key=b"d" * 48,
        )
        with pytest.raises(DeploymentError):
            worker().run_once(c, p["id"], trust=t)
        result = fake_worker().run_once(c, p["id"])
        assert result["mode"] == "fake-test" and result["verified"] is False
    with pytest.raises(DeploymentError):
        fake_worker().run_once(api, p["id"])
