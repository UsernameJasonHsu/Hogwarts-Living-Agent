import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from formal_identity import ORIGIN, identity, trust
from formal_support import real_formal
from hogwarts.entrypoints.bootstrap import create_formal_app
from hogwarts.entrypoints.deployment import DeploymentError
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import PrototypeRun, SandboxInstance
from hogwarts.reconciliation.docker import DockerResourceBackend
from hogwarts.reconciliation.planner import ResourceReconciler
from hogwarts.workspaces.manager import GitWorkspaceManager
from rc1_support import queued_workspace as queued_workspace
from recovery_support import real_orchestrator
from sqlalchemy import select
from test_rc1_worker import worker
from test_verification import HUMAN

REASON = "RC1 Linux Docker smoke requires pinned runtime, reader and review images"
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
    reason=REASON,
)


@pytest.mark.parametrize(
    "mode", ["normal", "wrong-key", "wrong-image", "wrong-release", "wrong-profile"]
)
def test_rc1_real_signed_operational_worker(queued_workspace, mode):
    from dataclasses import replace

    w = queued_workspace
    private = w["root"].parent / "rc1-private"
    controller = "rc1-" + uuid4().hex
    g, o = real_formal(w["manager"], private, controller)
    g.initialize(HUMAN, w["run"])
    d = identity(g)
    t = trust(d)
    updates = {
        "wrong-key": {"key": b"wrong-" + b"e" * 48},
        "wrong-image": {"controller_image_id": "sha256:" + "f" * 64},
        "wrong-release": {"release_sha256": "f" * 64},
        "wrong-profile": {"gateway_sha256": "f" * 64},
    }
    if mode != "normal":
        t = replace(t, **updates[mode])
    app = create_formal_app(g.factory.kw["bind"], gateway=g, mode="operational", deployment=d)
    try:
        with TestClient(
            app, base_url=ORIGIN, headers={"Authorization": headers("worker")["Authorization"]}
        ) as c:
            if mode != "normal":
                with pytest.raises(DeploymentError):
                    worker().run_once(c, w["proposal"], trust=t)
                with g.factory() as s:
                    assert s.get(PrototypeRun, w["run"]).lease_token is None
                    assert s.scalar(select(SandboxInstance)) is None
                return
            result = worker().run_once(c, w["proposal"], trust=t)
            assert result["status"] == "READY_FOR_HUMAN_REVIEW" and result["verified"] is False
            artifact = g.read_artifact(HUMAN, w["run"], result["bundle_artifact_id"])
            assert artifact.evidence.assessment.status == "unverified"
            assert hashlib.sha256(artifact.content).hexdigest() == result["bundle_sha256"]
            output = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
            if output:
                root = Path(output) / ("rc1-worker-" + w["run"])
                root.mkdir(parents=True, exist_ok=True)
                (root / "result.json").write_text(json.dumps(result, indent=2), encoding="utf8")
                (root / "deployment.json").write_text(
                    json.dumps(d.descriptor(), indent=2), encoding="utf8"
                )
                (root / "bundle.canonical.json").write_bytes(artifact.content)
    finally:
        # Trusted teardown of this disposable test Run, never an unknown resource.
        with g.factory() as s:
            generation = s.get(PrototypeRun, w["run"]).lease_generation
        DurableLeaseManager(g.factory).revoke(HUMAN, w["run"], generation, "rc1-test-finally")
        m = GitWorkspaceManager(g.factory, w["root"], (w["grant"],))
        raw = real_orchestrator(m, private, controller)
        reconciler = ResourceReconciler(m, backend=DockerResourceBackend(raw.controller, raw.qa))
        plan = reconciler.plan(HUMAN, w["run"], "rc1-finally")
        assert reconciler.execute(HUMAN, w["run"], plan.plan_id, plan.version).state == "COMPLETE"


@pytest.mark.parametrize("scope", ["root", "sandbox", "reader"])
def test_rc1_actual_docker_context_excludes_private_canaries(tmp_path, scope):
    # FROM scratch/COPY only: no workload execution, mounts, volumes or daemon prune.
    source = Path.cwd()
    context = tmp_path / "context"
    context.mkdir()
    export = tmp_path / "export"
    allow = json.loads((source / "validation/commit-allowlist.v1.json").read_text(encoding="utf8"))[
        "paths"
    ]
    folders = {"sandbox": "infra/sandbox", "reader": "infra/output-reader"}
    if scope == "root":
        expected = set(allow)
        inputs = [(source / name, name) for name in allow]
    else:
        folder = source / folders[scope]
        expected = {
            Path(name).name for name in allow if Path(name).parent.as_posix() == folders[scope]
        }
        inputs = [(folder / name, name) for name in expected]
    for original, name in inputs:
        dest = context / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, dest)
    probes = [
        ".env.production",
        "apps/api/.env.local",
        "keys/release.pem",
        "test-results/private.json",
        "outputs/private.log",
        ".git/config",
        ".aws/credentials",
        "apps/api/__pycache__/private.pyc",
        "apps/api/unlisted-config.json",
    ]
    for name in probes:
        dest = context / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"RC1-PRIVATE-CANARY-NOT-A-REAL-SECRET")
    dockerfile = tmp_path / "Context.Dockerfile"
    dockerfile.write_text("FROM scratch\nCOPY . /context/\n", encoding="utf8")
    p = subprocess.run(
        [
            shutil.which("docker"),
            "build",
            "--network=none",
            "--output",
            "type=local,dest=" + str(export),
            "-f",
            str(dockerfile),
            str(context),
        ],
        capture_output=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    actual = {
        p.relative_to(export / "context").as_posix()
        for p in (export / "context").rglob("*")
        if p.is_file()
    }
    assert actual == expected
    assert all(not (export / "context" / name).exists() for name in probes)
    output = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
    if output:
        root = Path(output) / ("rc1-context-" + uuid4().hex)
        root.mkdir(parents=True, exist_ok=True)
        (root / "audit.json").write_text(
            json.dumps(
                {
                    "context_scope": scope,
                    "copied_source_paths": sorted(actual),
                    "private_canaries_excluded": probes,
                    "real_secret_used": False,
                    "no_workload_execution": True,
                },
                indent=2,
            ),
            encoding="utf8",
        )
