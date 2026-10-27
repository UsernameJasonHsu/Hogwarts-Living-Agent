"""Production worker: pinned signed operational deployment, no legacy/fake fallback."""

import argparse
import os
import re
from uuid import UUID, uuid4

import httpx
from hogwarts.entrypoints.deployment import (
    PREFIX,
    DeploymentError,
    DeploymentTrust,
    normalized_origin,
    trust_from_environment,
)


def signed_request(client, trust, method, path, body=None, *, key=None):
    if type(trust) is not DeploymentTrust:
        raise DeploymentError("deployment_trust_required")
    if normalized_origin(str(client.base_url)) != trust.origin:
        raise DeploymentError("deployment_origin_mismatch")
    uuid_pattern = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
    route = re.fullmatch(
        re.escape(PREFIX)
        + r"/(?:deployment/challenge|prototypes/"
        + uuid_pattern
        + r"/context|runs/"
        + uuid_pattern
        + r"/(?:claim|execute))",
        path,
    )
    if not route or (method == "GET") != path.endswith("/context"):
        raise DeploymentError("deployment_nonformal_route_forbidden")
    headers = trust.request_headers(method, path, body, idempotency_key=key)
    try:
        args = {"headers": headers, "follow_redirects": False}
        if body is not None:
            args["json"] = body
        response = client.request(method, path, **args)
        if response.status_code != 200:
            raise DeploymentError("deployment_http_rejected")
        if len(response.content) > 1024 * 1024:
            raise DeploymentError("deployment_response_oversize")
        return trust.verify_response(
            method, path, body, headers["X-Hogwarts-Nonce"], response.json()
        )
    except (httpx.HTTPError, ValueError) as error:
        if isinstance(error, DeploymentError):
            raise
        raise DeploymentError("deployment_untrusted_or_uncertain_response") from None


def run_once(client, proposal_id: str, *, trust=None):
    if type(trust) is not DeploymentTrust:
        raise DeploymentError("deployment_trust_required")
    try:
        if str(UUID(proposal_id)) != proposal_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DeploymentError("deployment_invalid_proposal") from None
    proof = signed_request(
        client, trust, "POST", PREFIX + "/deployment/challenge", {"challenge": "deployment-v1"}
    )
    if proof != {"descriptor": trust.descriptor()}:
        raise DeploymentError("deployment_descriptor_mismatch")
    context = signed_request(client, trust, "GET", f"{PREFIX}/prototypes/{proposal_id}/context")
    queued = [run for run in context["runs"] if run["status"] == "QUEUED"]
    if len(queued) != 1:
        raise DeploymentError("deployment_expected_one_queued_run")
    run_id = queued[0]["id"]
    if not isinstance(run_id, str) or str(UUID(run_id)) != run_id:
        raise DeploymentError("deployment_invalid_run")
    lease = signed_request(
        client,
        trust,
        "POST",
        f"{PREFIX}/runs/{run_id}/claim",
        {"expected_version": context["proposal"]["version"]},
        key="claim-" + uuid4().hex,
    )
    result = signed_request(
        client,
        trust,
        "POST",
        f"{PREFIX}/runs/{run_id}/execute",
        {"expected_version": lease["version"], "lease_token": lease["lease_token"]},
        key="execute-" + uuid4().hex,
    )
    if result.get("mode") != "operational-deterministic" or result.get("verified") is not False:
        raise DeploymentError("deployment_nonoperational_result")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()
    trust = trust_from_environment()
    if args.base_url is not None and normalized_origin(args.base_url) != trust.origin:
        raise DeploymentError("deployment_origin_mismatch")
    token = os.environ["WORKER_API_TOKEN"]
    with httpx.Client(
        base_url=trust.origin,
        timeout=180,
        headers={"Authorization": "Bearer " + token},
        trust_env=False,
        follow_redirects=False,
    ) as client:
        result = run_once(client, args.proposal_id, trust=trust)
    print(
        {
            "id": result["id"],
            "status": result["status"],
            "mode": result["mode"],
            "verified": False,
        }
    )


if __name__ == "__main__":
    main()
