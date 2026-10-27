"""Real Linux Docker exact cleanup; Windows skips never count as isolation passes."""

import json
import os
import sys
from pathlib import Path

import pytest
from hogwarts.models import SandboxInstance
from hogwarts.outputs.driver import OutputController, OutputDockerDriver
from hogwarts.reconciliation.docker import DockerResourceBackend
from hogwarts.reconciliation.planner import ResourceReconciler
from hogwarts.sandboxes.contracts import RuntimeProfile
from sqlalchemy import select
from test_reconciliation import HUMAN, expire
from test_workspaces import create
from test_workspaces import workspace as workspace


@pytest.mark.parametrize(
    "scenario",
    [
        "exact",
        "active",
        "orphan_volume",
        "unknown_container",
        "unknown_volume",
        "foreign_attachment",
        "replacement",
        "immediate_replacement",
        "volume_replacement",
        "revoked",
        "daemon_failure",
    ],
)
def test_real_linux_precise_reconciliation(workspace, scenario, monkeypatch):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_OUTPUT_READER_IMAGE"):
        pytest.skip("Resource cleanup requires real Linux Docker and approved images")
    w = workspace
    image = os.environ["HOGWARTS_SANDBOX_IMAGE"]
    controller_id = "m242-" + w["run"].replace("-", "")
    driver = OutputDockerDriver(controller_id, os.environ["HOGWARTS_OUTPUT_READER_IMAGE"])
    controller = OutputController(
        w["manager"], driver, (RuntimeProfile("capture", image, lifetime_seconds=60),)
    )
    ws = create(w)
    controller.create(w["actor"], w["run"], ws.id, w["token"], "capture")
    with w["factory"]() as s:
        row = s.scalar(select(SandboxInstance))
    foreign = []
    original_call = driver.call
    backend = DockerResourceBackend(controller)
    r = ResourceReconciler(w["manager"], backend=backend)
    try:
        if scenario in {"exact", "active", "foreign_attachment", "revoked"}:
            controller.start(w["actor"], w["run"], w["token"])
        if scenario in {"orphan_volume", "volume_replacement"}:
            driver.call("container", "rm", "-f", row.container_id)
        if scenario.startswith("unknown_"):
            if scenario == "unknown_container":
                identifier = (
                    driver.call(
                        "container",
                        "create",
                        "--network",
                        "none",
                        "--label",
                        "hogwarts.controller=" + controller_id,
                        "--label",
                        "hogwarts.run=" + w["run"],
                        image,
                    )
                    .stdout.decode()
                    .strip()
                )
                foreign.append(("container", identifier))
            else:
                name = "hogwarts-test-unknown-" + row.id
                driver.call(
                    "volume",
                    "create",
                    "--label",
                    "hogwarts.controller=" + controller_id,
                    "--label",
                    "hogwarts.run=" + w["run"],
                    name,
                )
                foreign.append(("volume", name))
        if scenario == "foreign_attachment":
            identifier = (
                driver.call(
                    "container",
                    "create",
                    "--network",
                    "none",
                    "--mount",
                    "type=volume,src=" + driver.output_volume(row) + ",dst=/foreign,readonly",
                    image,
                )
                .stdout.decode()
                .strip()
            )
            foreign.append(("container", identifier))
        if scenario == "revoked":
            from hogwarts.leases.manager import DurableLeaseManager

            DurableLeaseManager(w["factory"]).revoke(HUMAN, w["run"], 1, "real-revoke")
        elif scenario != "active":
            expire(w)
        p = r.plan(HUMAN, w["run"], "real-plan")
        if scenario == "replacement":
            driver.call("container", "rm", "-f", row.container_id)
            identifier = (
                driver.call(
                    "container", "create", "--name", driver.name(row), "--network", "none", image
                )
                .stdout.decode()
                .strip()
            )
            foreign.append(("container", identifier))
        if scenario == "immediate_replacement":
            remove = backend.remove

            def replaced(resource):
                if resource.kind == "container":
                    driver.call("container", "rm", "-f", row.container_id)
                    identifier = (
                        driver.call(
                            "container",
                            "create",
                            "--name",
                            driver.name(row),
                            "--network",
                            "none",
                            image,
                        )
                        .stdout.decode()
                        .strip()
                    )
                    foreign.append(("container", identifier))
                remove(resource)

            monkeypatch.setattr(backend, "remove", replaced)
        if scenario == "volume_replacement":
            import time

            driver.call("volume", "rm", driver.volume(row))
            time.sleep(1.1)  # Docker volume CreatedAt may have only second precision.
            args = ["volume", "create", "--driver", "local"]
            for key, value in driver.labels(row).items():
                args += ["--label", key + "=" + value]
            for key, value in driver.volume_options().items():
                args += ["--opt", key + "=" + value]
            driver.call(*args, driver.volume(row))
        if scenario == "daemon_failure":

            def unavailable(*args, **kwargs):
                from hogwarts.service import DomainError

                raise DomainError(503, "docker_unavailable")

            monkeypatch.setattr(driver, "call", unavailable)
        q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
        monkeypatch.setattr(driver, "call", original_call)
        folder = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
        if folder:
            dest = Path(folder) / ("reconciliation-" + scenario + "-" + w["run"])
            dest.mkdir(parents=True)
            (dest / "dry-run.json").write_text(p.model_dump_json(indent=2))
            (dest / "cleanup-result.json").write_text(q.model_dump_json(indent=2))
            with w["factory"].begin() as s:
                run, proposal, project = r.context(s, HUMAN, w["run"])
                binding, observed = r.inventory.inspect(s, run, proposal, project)
            (dest / "after-observation.json").write_text(
                json.dumps(
                    {"binding": binding, "resources": [x.model_dump() for x in observed]}, indent=2
                )
            )
        if scenario in {"exact", "orphan_volume", "revoked"}:
            assert q.state == "COMPLETE", q
            assert driver.inspect(row) is None and driver.volume_info(row) is None
            assert driver.output_info(row) is None and driver.reader_info(row) is None
            assert not ws.path.exists()
        elif scenario == "active":
            assert q.state == "COMPLETE" and not q.completed
            assert driver.inspect(row)["Id"] == row.container_id
        elif scenario == "daemon_failure":
            assert q.state == "UNKNOWN" and not q.completed
            assert driver.inspect(row)["Id"] == row.container_id
        elif scenario == "immediate_replacement":
            assert q.state == "UNKNOWN" and not q.completed
            assert json.loads(driver.call("container", "inspect", foreign[0][1]).stdout)
        else:
            assert q.state == "QUARANTINED" and not q.completed
            if scenario not in {"replacement", "volume_replacement"}:
                assert driver.inspect(row)["Id"] == row.container_id
            for kind, identifier in foreign:
                assert json.loads(driver.call(kind, "inspect", identifier).stdout)
    finally:
        monkeypatch.setattr(driver, "call", original_call)
        # Test-owned exact IDs only. Replacement/unknown resources are test fixtures,
        # never adopted by the reconciler, and removed explicitly here.
        for kind, identifier in foreign:
            args = (
                (kind, "rm", "-f", identifier) if kind == "container" else (kind, "rm", identifier)
            )
            driver.call(*args)
        controller.destroy(HUMAN, w["run"])


def test_real_linux_qa_orphans_preserve_permanent_evidence(workspace, monkeypatch):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_REVIEW_QA_IMAGE"):
        pytest.skip("QA orphan cleanup requires real Linux Docker and approved images")
    import hashlib

    from hogwarts.artifacts.store import LocalArtifactStore
    from hogwarts.outputs.contracts import OutputGrant
    from hogwarts.outputs.importer import LocalOutputImporter
    from hogwarts.patches.builder import LocalGitPatchBuilder
    from hogwarts.qa.driver import QADockerDriver
    from hogwarts.qa.profiles import QAProfileRegistry
    from hogwarts.qa.runner import LocalTrustedQARunner
    from hogwarts.verification.orchestrator import LocalPrototypeVerificationOrchestrator
    from hogwarts.verification.profiles import ReviewQAProfile
    from test_verification import verify

    w = workspace
    root = w["root"].parent
    controller_id = "m242-" + w["run"].replace("-", "")
    driver = OutputDockerDriver(controller_id, os.environ["HOGWARTS_OUTPUT_READER_IMAGE"])
    c = OutputController(
        w["manager"],
        driver,
        (RuntimeProfile("capture", os.environ["HOGWARTS_SANDBOX_IMAGE"], lifetime_seconds=60),),
    )
    store = LocalArtifactStore(w["manager"], root / "store")
    importer = LocalOutputImporter(c, store, root / "import", (OutputGrant("capture", "src"),))
    builder = LocalGitPatchBuilder(importer, root / "patch")
    qa = LocalTrustedQARunner(
        builder,
        root / "qa",
        QADockerDriver(controller_id),
        QAProfileRegistry([ReviewQAProfile("acceptance", os.environ["HOGWARTS_REVIEW_QA_IMAGE"])]),
    )
    o = LocalPrototypeVerificationOrchestrator(qa, root / "verification")
    original = qa.driver.destroy
    try:
        # Inject a controller cleanup omission, leaving actual holder/job/volume.
        monkeypatch.setattr(qa.driver, "destroy", lambda row: None)
        result = verify((w, o, driver, None))
        monkeypatch.setattr(qa.driver, "destroy", original)
        assert result.status == "READY_FOR_HUMAN_REVIEW"
        protected = [
            store.objects.root,
            importer.vault.objects.root,
            builder.objects.root,
            qa.vault.objects.root,
            o.vault.objects.root,
        ]

        def hashes():
            return {
                str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                for folder in protected
                for p in folder.rglob("*")
                if p.is_file()
            }

        before = hashes()
        assert before
        expire(w)
        r = ResourceReconciler(w["manager"], backend=DockerResourceBackend(c, qa))
        p = r.plan(HUMAN, w["run"], "qa-orphans")
        assert len([x for x in p.resources if x.kind == "container"]) == 2
        q = r.execute(HUMAN, w["run"], p.plan_id, p.version)
        assert q.state == "COMPLETE", q
        assert hashes() == before
        assert not [
            x
            for x in r.plan(HUMAN, w["run"], "after-qa").resources
            if x.disposition == "CLEANUP_ELIGIBLE"
        ]
        folder = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
        if folder:
            dest = Path(folder) / ("reconciliation-qa-orphans-" + w["run"])
            dest.mkdir(parents=True)
            (dest / "dry-run.json").write_text(p.model_dump_json(indent=2))
            (dest / "cleanup-result.json").write_text(q.model_dump_json(indent=2))
            (dest / "preserved-evidence-hashes.json").write_text(json.dumps(before, indent=2))
    finally:
        monkeypatch.setattr(qa.driver, "destroy", original)
        o.cleanup(HUMAN, w["run"])


@pytest.mark.parametrize("mode", ["symlink", "fifo"])
def test_linux_private_root_link_and_special_file(workspace, mode):
    if sys.platform != "linux":
        pytest.skip("Linux symlink/FIFO negative test")
    w = workspace
    ws = create(w)
    path = ws.path / "src/unsafe"
    if mode == "symlink":
        path.symlink_to(w["repo"])
    else:
        os.mkfifo(path)
    try:
        expire(w)
        r = ResourceReconciler(w["manager"])
        p = r.plan(HUMAN, w["run"], "unsafe-tree")
        assert any(x.disposition == "QUARANTINE" for x in p.resources)
        assert r.execute(HUMAN, w["run"], p.plan_id, p.version).state == "QUARANTINED"
        assert path.lstat()
    finally:
        path.unlink()
