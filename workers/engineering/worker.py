"""Run one explicitly selected M1 fake job through authenticated API commands."""

import argparse
import os
from uuid import uuid4

import httpx


def run_once(client, proposal_id: str):
    proposal = client.get(f"/api/v1/prototypes/{proposal_id}")
    proposal.raise_for_status()
    runs = client.get(f"/api/v1/prototypes/{proposal_id}/runs")
    runs.raise_for_status()
    queued = [run for run in runs.json() if run["status"] == "QUEUED"]
    if len(queued) != 1:
        raise RuntimeError("expected one queued fake run")
    run_id = queued[0]["id"]
    claim = client.post(
        f"/api/v1/runs/{run_id}/claim",
        json={"expected_version": proposal.json()["version"]},
        headers={"Idempotency-Key": "claim-" + uuid4().hex},
    )
    claim.raise_for_status()
    lease = claim.json()
    result = client.post(
        f"/api/v1/runs/{run_id}/execute",
        json={"expected_version": lease["version"], "lease_token": lease["lease_token"]},
        headers={"Idempotency-Key": "execute-" + uuid4().hex},
    )
    result.raise_for_status()
    return result.json()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    token = os.environ["WORKER_API_TOKEN"]
    with httpx.Client(
        base_url=args.base_url, timeout=30, headers={"Authorization": "Bearer " + token}
    ) as client:
        result = run_once(client, args.proposal_id)
    print({"id": result["id"], "status": result["status"], "mode": "fake", "verified": False})


if __name__ == "__main__":
    main()
