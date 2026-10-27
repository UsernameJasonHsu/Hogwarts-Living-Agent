"""Out-of-band deployment trust, separate from M1 action authorization."""

import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PREFIX = "/api/v1/formal-worker"
MAX_CLOCK_SKEW = 30
_GATE = None


class DeploymentError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def deny(code):
    raise DeploymentError(code)


def normalized_origin(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or value.strip() != value
        or any(ord(c) < 33 for c in value)
    ):
        deny("deployment_invalid_origin")
    try:
        p = urlsplit(value)
        host = p.hostname
        port = p.port
        if (
            not host
            or p.username
            or p.password
            or p.query
            or p.fragment
            or p.path not in {"", "/"}
            or p.scheme not in {"http", "https"}
        ):
            deny("deployment_invalid_origin")
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if p.scheme == "http" and not loopback:
            deny("deployment_tls_required")
        host = "[" + host + "]" if ":" in host else host
        suffix = "" if port in {None, 443 if p.scheme == "https" else 80} else ":" + str(port)
        return p.scheme + "://" + host + suffix
    except ValueError as error:
        if isinstance(error, DeploymentError):
            raise
        deny("deployment_invalid_origin")


def release_identity():
    global _GATE
    if _GATE is None:
        path = Path(__file__).resolve().parents[4] / "scripts/ci_gate.py"
        spec = importlib.util.spec_from_file_location("hogwarts_trusted_release_gate", path)
        _GATE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_GATE)
    try:
        return _GATE.release_identity()
    except (ValueError, OSError, UnicodeError):
        deny("deployment_source_not_validated")


def gateway_fingerprint(g):
    return digest(
        {
            "profile": g.profile_name,
            "runtime": g.operational.configurations[g.profile_name],
            "quota": g.quota.profiles[g.profile_name].model_dump(),
            "controllers": [
                g.orchestrator.controller.driver.controller_id,
                g.orchestrator.qa.driver.controller_id,
            ],
            "roots": [str(p) for p in g.roots(g.orchestrator)],
            "preserve_budget": g.orchestrator.preserve_budget,
        }
    )


def sign(key, domain, payload):
    return hmac.new(
        key,
        ("hogwarts.formal-worker." + domain + ".v1\0").encode() + canonical(payload),
        hashlib.sha256,
    ).hexdigest()


@dataclass(frozen=True)
class DeploymentTrust:
    origin: str
    deployment_id: str
    controller_image_id: str
    release_sha256: str
    gateway_sha256: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "origin", normalized_origin(self.origin))
        if any(
            type(x) is not str
            for x in (
                self.deployment_id,
                self.controller_image_id,
                self.release_sha256,
                self.gateway_sha256,
            )
        ):
            deny("deployment_invalid_identity")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", self.deployment_id):
            deny("deployment_invalid_identity")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", self.controller_image_id):
            deny("deployment_unpinned_image")
        if any(
            not re.fullmatch(r"[a-f0-9]{64}", x) for x in (self.release_sha256, self.gateway_sha256)
        ):
            deny("deployment_invalid_digest")
        if type(self.key) is not bytes or not 32 <= len(self.key) <= 256 or not self.key.isascii():
            deny("deployment_invalid_key")
        if any(
            token and hmac.compare_digest(self.key, token.encode())
            for token in (os.getenv("WORKER_API_TOKEN", ""), os.getenv("HUMAN_API_TOKEN", ""))
        ):
            deny("deployment_separate_key_required")

    def descriptor(self):
        return {
            "schema_version": 1,
            "mode": "operational",
            "runtime": "linux-container",
            "origin": self.origin,
            "deployment_id": self.deployment_id,
            "controller_image_id": self.controller_image_id,
            "release_sha256": self.release_sha256,
            "gateway_sha256": self.gateway_sha256,
        }

    @property
    def fingerprint(self):
        return digest(self.descriptor())

    def request_headers(self, method, path, body, *, idempotency_key=None):
        nonce = secrets.token_hex(16)
        stamp = str(int(time.time()))
        payload = {
            "deployment": self.fingerprint,
            "method": method.upper(),
            "path": path,
            "body_sha256": digest(body),
            "nonce": nonce,
            "timestamp": stamp,
            "idempotency_key": idempotency_key,
        }
        headers = {
            "X-Hogwarts-Deployment": self.fingerprint,
            "X-Hogwarts-Nonce": nonce,
            "X-Hogwarts-Timestamp": stamp,
            "X-Hogwarts-Signature": sign(self.key, "request", payload),
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def verify_request(self, method, path, body, headers, origin):
        if normalized_origin(origin) != self.origin:
            deny("deployment_origin_mismatch")
        nonce = headers.get("x-hogwarts-nonce", "")
        stamp = headers.get("x-hogwarts-timestamp", "")
        if (
            not re.fullmatch(r"[a-f0-9]{32}", nonce)
            or not re.fullmatch(r"[0-9]{1,12}", stamp)
            or abs(time.time() - int(stamp)) > MAX_CLOCK_SKEW
        ):
            deny("deployment_stale_request")
        if headers.get("x-hogwarts-deployment") != self.fingerprint:
            deny("deployment_identity_mismatch")
        expected = sign(
            self.key,
            "request",
            {
                "deployment": self.fingerprint,
                "method": method.upper(),
                "path": path,
                "body_sha256": digest(body),
                "nonce": nonce,
                "timestamp": stamp,
                "idempotency_key": headers.get("idempotency-key"),
            },
        )
        observed = headers.get("x-hogwarts-signature", "")
        if not re.fullmatch(r"[a-f0-9]{64}", observed) or not hmac.compare_digest(
            expected, observed
        ):
            deny("deployment_invalid_signature")
        return nonce

    def response(self, method, path, body, nonce, data):
        envelope = {
            "schema_version": 1,
            "deployment_sha256": self.fingerprint,
            "nonce": nonce,
            "issued_at": int(time.time()),
            "data": data,
        }
        envelope["signature"] = sign(
            self.key,
            "response",
            {"envelope": envelope, "method": method, "path": path, "body_sha256": digest(body)},
        )
        return envelope

    def verify_response(self, method, path, body, nonce, envelope):
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema_version",
            "deployment_sha256",
            "nonce",
            "issued_at",
            "data",
            "signature",
        }:
            deny("deployment_invalid_response")
        if (
            envelope["schema_version"] != 1
            or envelope["deployment_sha256"] != self.fingerprint
            or envelope["nonce"] != nonce
            or type(envelope["issued_at"]) is not int
            or abs(time.time() - envelope["issued_at"]) > MAX_CLOCK_SKEW
        ):
            deny("deployment_response_binding_mismatch")
        signature = envelope["signature"]
        unsigned = {k: v for k, v in envelope.items() if k != "signature"}
        expected = sign(
            self.key,
            "response",
            {"envelope": unsigned, "method": method, "path": path, "body_sha256": digest(body)},
        )
        if (
            not isinstance(signature, str)
            or not re.fullmatch(r"[a-f0-9]{64}", signature)
            or not hmac.compare_digest(expected, signature)
        ):
            deny("deployment_invalid_response_signature")
        return envelope["data"]


@dataclass(frozen=True)
class DeploymentIdentity(DeploymentTrust):
    def validate_gateway(self, gateway):
        from hogwarts.outputs.driver import OutputDockerDriver
        from hogwarts.qa.driver import QADockerDriver

        if (
            type(gateway.orchestrator.controller.driver) is not OutputDockerDriver
            or type(gateway.orchestrator.qa.driver) is not QADockerDriver
        ):
            deny("deployment_linux_runtime_required")
        if (
            gateway_fingerprint(gateway) != self.gateway_sha256
            or gateway.orchestrator.preserve_budget is not True
        ):
            deny("deployment_gateway_mismatch")
        if release_identity()["release_sha256"] != self.release_sha256:
            deny("deployment_release_mismatch")


def trust_from_environment():
    names = {
        "origin": "HOGWARTS_WORKER_ORIGIN",
        "deployment_id": "HOGWARTS_DEPLOYMENT_ID",
        "controller_image_id": "HOGWARTS_CONTROLLER_IMAGE_ID",
        "release_sha256": "HOGWARTS_RELEASE_SHA256",
        "gateway_sha256": "HOGWARTS_GATEWAY_SHA256",
    }
    if any(not os.getenv(v) for v in [*names.values(), "HOGWARTS_DEPLOYMENT_KEY"]):
        deny("deployment_trust_not_configured")
    try:
        key = os.environ["HOGWARTS_DEPLOYMENT_KEY"].encode("ascii")
    except UnicodeError:
        deny("deployment_invalid_key")
    if key.startswith(b"rc1-inprocess-test-"):
        deny("deployment_test_key_forbidden_in_cli")
    return DeploymentTrust(**{k: os.environ[v] for k, v in names.items()}, key=key)
