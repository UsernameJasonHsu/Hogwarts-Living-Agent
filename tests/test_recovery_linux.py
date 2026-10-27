"""Real Linux worker/controller SIGKILL and durable recovery across new processes."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import BudgetReservation, IdempotencyRecord, PrototypeRun, WorkflowEvent
from hogwarts.reconciliation.docker import DockerResourceBackend
from hogwarts.reconciliation.planner import ResourceReconciler
from hogwarts.recovery.coordinator import CrashRecoveryCoordinator
from recovery_support import real_orchestrator
from sqlalchemy import func, select
from test_verification import HUMAN
from test_workspaces import workspace as workspace


@pytest.mark.parametrize(
    "mode",
    ["verification_sealed", "db_commit", "artifact_partial", "qa_unsealed", "generation_changed"],
)
def test_real_linux_process_crash_recovery(workspace, mode):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_REVIEW_QA_IMAGE"):
        pytest.skip("Real process crash recovery requires Linux Docker and approved images")
    w = workspace
    root = w["root"].parent
    controller_id = "m243-" + w["run"].replace("-", "")
    o = real_orchestrator(w["manager"], root, controller_id)
    marker = root / "crash-marker.json"
    config = root / "controller.private.json"
    config.write_text(
        json.dumps(
            {
                "database": os.environ["DATABASE_URL"],
                "run": w["run"],
                "token": w["token"],
                "project": w["grant"].project_id,
                "repository": str(w["repo"]),
                "root": str(root),
                "controller": controller_id,
                "marker": str(marker),
                "docker_executable": o.controller.driver.executable,
            }
        ),
        encoding="utf8",
    )
    config.chmod(0o600)
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "DOCKER_CONFIG": "/nonexistent",
        "PYTHONPATH": str(Path("apps/api").resolve()),
        **{key: value for key, value in os.environ.items() if key.startswith("HOGWARTS_")},
    }
    scenario = "verification_sealed" if mode == "generation_changed" else mode
    try:
        result = subprocess.run(
            [sys.executable, "tests/recovery_process.py", str(config), scenario],
            env=env,
            capture_output=True,
            timeout=150,
            shell=False,
        )
        assert result.returncode == -signal.SIGKILL, result.stdout.decode(errors="replace")
        observed = json.loads(marker.read_text())
        assert observed["pid"] != os.getpid()
        backend = DockerResourceBackend(o.controller, o.qa)
        recovery = CrashRecoveryCoordinator(o, backend=backend, stale_seconds=0)
        if mode == "generation_changed":
            DurableLeaseManager(w["factory"]).revoke(HUMAN, w["run"], 1, "after-crash")
        actor = HUMAN if mode in {"qa_unsealed", "generation_changed"} else w["actor"]
        token = None if actor == HUMAN else w["token"]
        plan = recovery.recover(actor, w["run"], "after-process-crash", token)
        done = recovery.execute(actor, w["run"], plan.recovery_id, plan.version, token)
        with w["factory"]() as s:
            count = s.scalar(
                select(func.count())
                .select_from(WorkflowEvent)
                .where(
                    WorkflowEvent.aggregate_id == w["run"],
                    WorkflowEvent.event_type == "qa.executing",
                )
            )
            assert count == 1
            run = s.get(PrototypeRun, w["run"])
            budget = s.scalar(select(BudgetReservation))
            if mode in {"verification_sealed", "db_commit"}:
                assert done.state == "COMPLETE" and run.status == "READY_FOR_HUMAN_REVIEW", done
                assert budget.status == "RELEASED" and budget.actual_cents == 0
            elif mode == "artifact_partial":
                assert done.state == "COMPLETE", done
                qa = s.scalar(
                    select(IdempotencyRecord).where(IdempotencyRecord.key.like("qa-runner.v1:%"))
                )
                assert qa.response["state"] == "READY" and budget.status == "RESERVED"
            else:
                assert done.state == "MANUAL_REQUIRED" and budget.status == "RESERVED", done
        folder = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
        if folder:
            dest = Path(folder) / ("recovery-" + mode + "-" + w["run"])
            dest.mkdir(parents=True)
            (dest / "process-observation.json").write_text(
                json.dumps(
                    {**observed, "returncode": result.returncode, "actual_sigkill": True}, indent=2
                )
            )
            (dest / "dry-run.json").write_text(plan.model_dump_json(indent=2))
            (dest / "recovery-result.json").write_text(done.model_dump_json(indent=2))
    finally:
        # Cleanup is a separate explicit M2.4.2 action, never a Recovery effect.
        with w["factory"]() as s:
            generation = s.get(PrototypeRun, w["run"]).lease_generation
        DurableLeaseManager(w["factory"]).revoke(HUMAN, w["run"], generation, "test-cleanup")
        cleaner = ResourceReconciler(
            w["manager"], backend=DockerResourceBackend(o.controller, o.qa)
        )
        p = cleaner.plan(HUMAN, w["run"], "explicit-test-cleanup")
        cleaned = cleaner.execute(HUMAN, w["run"], p.plan_id, p.version)
        assert cleaned.state == "COMPLETE", cleaned
