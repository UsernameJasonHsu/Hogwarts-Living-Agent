"""Explicit in-process canonical-fake test helper. Never a production CLI worker."""

from fastapi.testclient import TestClient
from hogwarts.entrypoints.bootstrap import _CANONICAL_FACTORY, sealed_fake_check
from hogwarts.entrypoints.deployment import DeploymentError


def run_once(client, proposal_id):
    if not isinstance(client, TestClient):
        raise DeploymentError("fake_test_inprocess_client_required")
    state = client.app.state
    if (
        getattr(state, "_canonical_factory", None) is not _CANONICAL_FACTORY
        or getattr(state, "_canonical_mode", None) != "fake"
    ):
        raise DeploymentError("fake_test_canonical_mode_required")
    sealed_fake_check()(state.adapters)
    p = client.get(f"/api/v1/prototypes/{proposal_id}")
    p.raise_for_status()
    rows = client.get(f"/api/v1/prototypes/{proposal_id}/runs")
    rows.raise_for_status()
    queued = [r for r in rows.json() if r["status"] == "QUEUED"]
    if len(queued) != 1:
        raise DeploymentError("fake_test_expected_one_queued_run")
    run = queued[0]["id"]
    from uuid import uuid4

    lease = client.post(
        f"/api/v1/runs/{run}/claim",
        json={"expected_version": p.json()["version"]},
        headers={"Idempotency-Key": "fake-claim-" + uuid4().hex},
    )
    lease.raise_for_status()
    result = client.post(
        f"/api/v1/runs/{run}/execute",
        json={
            "expected_version": lease.json()["version"],
            "lease_token": lease.json()["lease_token"],
        },
        headers={"Idempotency-Key": "fake-execute-" + uuid4().hex},
    )
    result.raise_for_status()
    return {**result.json(), "mode": "fake-test", "verified": False}


if __name__ == "__main__":
    raise SystemExit("fake_worker is an explicit in-process test helper, not a CLI")
