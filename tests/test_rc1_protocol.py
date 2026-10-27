import time

import pytest
from hogwarts.entrypoints.deployment import (
    PREFIX,
    DeploymentError,
    DeploymentTrust,
    normalized_origin,
)
from httpx import Headers


def trusted():
    return DeploymentTrust(
        origin="https://testserver",
        deployment_id="rc1-test",
        controller_image_id="sha256:" + "a" * 64,
        release_sha256="b" * 64,
        gateway_sha256="c" * 64,
        key=b"d" * 48,
    )


def test_rc1_signed_request_and_response():
    t = trusted()
    path = PREFIX + "/deployment/challenge"
    body = {"challenge": "deployment-v1"}
    headers = Headers(t.request_headers("POST", path, body))
    nonce = t.verify_request("POST", path, body, headers, t.origin)
    data = {"descriptor": t.descriptor()}
    response = t.response("POST", path, body, nonce, data)
    assert t.verify_response("POST", path, body, nonce, response) == data
    assert t.key.decode() not in repr(t)


@pytest.mark.parametrize(
    "part",
    [
        "origin",
        "path",
        "method",
        "body",
        "signature",
        "nonce",
        "timestamp",
        "deployment",
        "idempotency",
    ],
)
def test_rc1_request_binding_rejects_tampering(part):
    t = trusted()
    path = PREFIX + "/runs/id/claim"
    body = {"expected_version": 3}
    method = "POST"
    origin = t.origin
    h = Headers(t.request_headers(method, path, body, idempotency_key="original"))
    if part == "origin":
        origin = "https://other.example"
    elif part == "path":
        path += "x"
    elif part == "method":
        method = "GET"
    elif part == "body":
        body = {"expected_version": 4}
    else:
        h[
            {
                "signature": "x-hogwarts-signature",
                "nonce": "x-hogwarts-nonce",
                "timestamp": "x-hogwarts-timestamp",
                "deployment": "x-hogwarts-deployment",
                "idempotency": "idempotency-key",
            }[part]
        ] = str(int(time.time()) - 1000) if part == "timestamp" else "changed"
    with pytest.raises(DeploymentError):
        t.verify_request(method, path, body, h, origin)


@pytest.mark.parametrize(
    "part", ["data", "nonce", "time", "deployment", "signature", "schema", "extra"]
)
def test_rc1_response_binding_rejects_forgery(part):
    t = trusted()
    path = PREFIX + "/deployment/challenge"
    body = {}
    nonce = "a" * 32
    r = t.response("POST", path, body, nonce, {"mode": "operational"})
    key = {
        "data": "data",
        "nonce": "nonce",
        "time": "issued_at",
        "deployment": "deployment_sha256",
        "signature": "signature",
        "schema": "schema_version",
        "extra": "extra",
    }[part]
    r[key] = int(time.time()) - 1000 if part == "time" else "forged"
    with pytest.raises(DeploymentError):
        t.verify_response("POST", path, body, nonce, r)


@pytest.mark.parametrize(
    "origin",
    [
        "http://remote.example",
        pytest.param("https://user:" + "pass@example.com", id="userinfo-forbidden"),
        "https://example.com/path",
        "https://example.com?key=value",
        "https://example.com/#fragment",
        " https://example.com",
        "ftp://example.com",
    ],
)
def test_rc1_untrusted_origins_rejected(origin):
    with pytest.raises(DeploymentError):
        normalized_origin(origin)


def test_rc1_loopback_http_is_explicit():
    assert normalized_origin("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
