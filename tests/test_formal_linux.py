import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from formal_identity import identity
from formal_support import real_formal
from hogwarts.entrypoints.bootstrap import create_formal_app
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import (
    BudgetReservation,
    Proposal,
    PrototypeRun,
    SandboxInstance,
    now,
)
from hogwarts.reconciliation.docker import DockerResourceBackend
from hogwarts.reconciliation.planner import ResourceReconciler
from hogwarts.service import DomainError
from hogwarts.workspaces.manager import GitWorkspaceManager
from recovery_support import real_orchestrator
from sqlalchemy import select
from test_verification import HUMAN
from test_workspaces import workspace as workspace

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or not all(
        os.getenv(n)
        for n in (
            "HOGWARTS_SANDBOX_IMAGE",
            "HOGWARTS_OUTPUT_READER_IMAGE",
            "HOGWARTS_REVIEW_QA_IMAGE",
        )
    ),
    reason="Formal E2E requires Linux Docker and pinned runtime/reader/review images",
)


def save(name, data, run_id):
    folder = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
    if folder:
        target = Path(folder) / ("formal-" + run_id)
        target.mkdir(parents=True, exist_ok=True)
        (target / (name + ".json")).write_text(json.dumps(data, indent=2), encoding="utf8")


@pytest.mark.parametrize("mode", ["normal", "quota", "cancel", "restart"])
def test_real_linux_formal_entrypoint(workspace, monkeypatch, mode):
    w = workspace
    private = w["root"].parent / "formal-private"
    controller_id = "m25-" + uuid4().hex
    g, o = real_formal(w["manager"], private, controller_id, slots=1 if mode == "quota" else 2)
    g.initialize(HUMAN, w["run"])
    with w["factory"]() as s:
        version = s.get(Proposal, w["proposal"]).version
    body = {"expected_version": version, "lease_token": w["token"]}
    app = create_formal_app(
        w["factory"].kw["bind"], gateway=g, mode="operational", deployment=identity(g)
    )
    try:
        if mode == "quota":
            with g.boundary.scope(w["actor"], w["run"], "admission"):
                g.quota.reserve(w["actor"], w["run"], "occupy", w["token"], "prototype")
        if mode == "cancel":

            def cancel(*args, **kwargs):
                with w["factory"]() as s:
                    current = s.get(Proposal, w["proposal"]).version
                with TestClient(app) as client:
                    response = client.post(
                        f"/api/v1/prototypes/{w['proposal']}/cancel",
                        json={"expected_version": current},
                        headers=headers("human", "cancel-real"),
                    )
                assert response.status_code == 200, response.text
                raise DomainError(409, "fixture_cancelled_before_qa")

            monkeypatch.setattr(o.qa, "run", cancel)
        if mode == "restart":
            config = w["root"].parent / "formal.private.json"
            marker = w["root"].parent / "formal-marker.json"
            cfg = {
                "database": g.factory.kw["bind"].url.render_as_string(hide_password=False),
                "repository": str(w["repo"]),
                "project": w["grant"].project_id,
                "repo_ref": w["grant"].repo_ref,
                "workspace_root": str(w["root"]),
                "private_root": str(private),
                "controller": controller_id,
                "docker": o.controller.driver.executable,
                "run": w["run"],
                "token": w["token"],
                "version": version,
                "marker": str(marker),
            }
            config.write_text(json.dumps(cfg), encoding="utf8")
            config.chmod(0o600)
            env = {
                "PATH": os.defpath,
                "HOME": "/nonexistent",
                "PYTHONPATH": "/app/apps/api:/app/tests",
            }
            for key in (
                "HOGWARTS_SANDBOX_IMAGE",
                "HOGWARTS_OUTPUT_READER_IMAGE",
                "HOGWARTS_REVIEW_QA_IMAGE",
            ):
                env[key] = os.environ[key]
            killed = subprocess.run(
                [sys.executable, "tests/formal_process.py", str(config)],
                env=env,
                capture_output=True,
                timeout=120,
            )
            assert killed.returncode == -signal.SIGKILL, (killed.returncode, killed.stdout.decode())
            with pytest.raises(DomainError, match="formal_execution_uncertain"):
                g.execute(w["actor"], w["run"], version, w["token"], "process-crash")
            with w["factory"]() as s:
                assert s.scalar(select(SandboxInstance)) is None
                assert g.quota.total(g.quota.ledger(s, HUMAN, lock=False)[1]).slots == 1
                assert s.scalar(select(BudgetReservation)).status == "RESERVED"
            save(
                "process",
                {
                    **json.loads(marker.read_text()),
                    "returncode": killed.returncode,
                    "replayed": False,
                },
                w["run"],
            )
            return
        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/runs/{w['run']}/execute",
                json=body,
                headers=headers("worker", "formal-real"),
            )
        if mode in {"quota", "cancel"}:
            assert response.status_code == 409, response.text
            with w["factory"]() as s:
                b = s.scalar(select(BudgetReservation))
                assert b.status == "RESERVED" and b.actual_cents is None
                assert g.quota.total(g.quota.ledger(s, HUMAN, lock=False)[1]).slots == 1
            save(
                "denial",
                {
                    "mode": mode,
                    "status": response.status_code,
                    "detail": response.json(),
                    "commitments_retained": True,
                },
                w["run"],
            )
            return
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "READY_FOR_HUMAN_REVIEW" and result["verified"] is False
        bundle = g.read_artifact(HUMAN, w["run"], result["bundle_artifact_id"])
        assert hashlib.sha256(bundle.content).hexdigest() == result["bundle_sha256"]
        assert bundle.evidence.assessment.status == "unverified"
        g.review(
            HUMAN,
            w["run"],
            result["verification_id"],
            result["bundle_sha256"],
            result["proposal_version"],
            "REJECTED",
            "formal-review",
        )
        with w["factory"].begin() as s:
            s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(days=2)
        plan = g.cleanup_plan(HUMAN, w["run"], "formal-gc")
        approved = g.cleanup_approve(HUMAN, w["run"], plan.plan_id, plan.version)
        cleaned = g.cleanup_execute(HUMAN, w["run"], plan.plan_id, approved.version)
        assert cleaned.state == "COMPLETE", cleaned
        health = g.snapshot(HUMAN, w["run"])
        with w["factory"]() as s:
            r = g.quota.ledger(s, HUMAN, lock=False)[1].reservations[0]
        released = g.release_quota(HUMAN, w["run"], r.reservation_id, health["quota_version"])
        assert released.state == "RELEASED"
        save("result", result, w["run"])
        save("bundle", json.loads(bundle.content), w["run"])
        save("cleanup", cleaned.model_dump(mode="json"), w["run"])
    finally:
        # Test-only trusted teardown, not an approved application/Agent entrypoint.
        # Reconstruct unbound adapters for THIS known disposable fixture Run only.
        with w["factory"]() as s:
            generation = s.get(PrototypeRun, w["run"]).lease_generation
        DurableLeaseManager(g.factory).revoke(HUMAN, w["run"], generation, "formal-finally")
        m = GitWorkspaceManager(g.factory, w["root"], (w["grant"],))
        raw = real_orchestrator(m, private, controller_id)
        cleanup = ResourceReconciler(m, backend=DockerResourceBackend(raw.controller, raw.qa))
        plan = cleanup.plan(HUMAN, w["run"], "formal-finally")
        assert cleanup.execute(HUMAN, w["run"], plan.plan_id, plan.version).state == "COMPLETE"
