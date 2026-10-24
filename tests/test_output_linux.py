"""Opt-in real Docker Linux integration; skipped platforms are never claimed verified."""

import copy
import json
import os
import sys

import pytest
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.auth import Principal
from hogwarts.models import SandboxInstance
from hogwarts.outputs.contracts import OutputGrant
from hogwarts.outputs.driver import OutputController, OutputDockerDriver
from hogwarts.outputs.importer import LocalOutputImporter
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.service import DomainError
from test_workspaces import workspace as workspace


@pytest.fixture
def linux_output(workspace):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_OUTPUT_READER_IMAGE"):
        pytest.skip(
            "Real Linux output reader requires pinned image; Windows is development host only"
        )
    w = workspace
    info = w["manager"].create(w["actor"], w["run"], w["token"])
    driver = OutputDockerDriver("m2-3-2-verification", os.environ["HOGWARTS_OUTPUT_READER_IMAGE"])
    profile = RuntimeProfile(
        "output-verify", os.environ["HOGWARTS_SANDBOX_IMAGE"], lifetime_seconds=60
    )
    controller = OutputController(w["manager"], driver, (profile,))
    sb = controller.create(w["actor"], w["run"], info.id, w["token"], profile.name)
    store = LocalArtifactStore(w["manager"], w["root"].parent / "output-artifacts")
    importer = LocalOutputImporter(
        controller, store, w["root"].parent / "output-capsules", (OutputGrant(profile.name, "src"),)
    )
    try:
        controller.start(w["actor"], w["run"], w["token"])
        yield w, info, sb, controller, store, importer, profile
    finally:
        controller.destroy(Principal("human:local-owner", "local-owner", "human"), w["run"])
        with w["factory"]() as session:
            row = session.get(SandboxInstance, sb.id)
            assert driver.output_info(row) is None and driver.reader_info(row) is None
            assert driver.inspect(row) is None and driver.volume_info(row) is None


def import_output(o):
    w, ws, sb, c, _, i, _ = o
    return c.import_outputs(i, w["actor"], w["run"], ws.id, sb.id, w["token"])


def test_real_linux_stopped_producer_ro_reader_integrity_and_cleanup(linux_output):
    o = linux_output
    w, ws, sb, c, store, _, profile = o
    # A running producer is never an import source, including before execute.
    with pytest.raises(DomainError):
        import_output(o)
    result = c.execute(w["actor"], w["run"], w["token"], "capture")
    assert result.state == "COMPLETED"
    security = result.result["output"]
    assert (
        security["uid"] == 10001
        and security["network_blocked"]
        and security["host_secrets_unavailable"]
    )
    with w["factory"]() as session:
        row = session.get(SandboxInstance, sb.id)
        producer = c.driver.verify(row, profile)
        reader = c.driver.reader_info(row)
        assert not producer["State"]["Running"] and reader["State"]["Running"]
        assert reader["Mounts"][0]["RW"] is False
        host = reader["HostConfig"]
        assert host["ReadonlyRootfs"] and host["NetworkMode"] == "none"
        assert (
            host["Memory"] == 67108864 and host["NanoCpus"] == 250000000 and host["PidsLimit"] == 8
        )
        assert host["CapDrop"] == ["ALL"] and host["SecurityOpt"] == ["no-new-privileges"]
    first = import_output(o)
    assert import_output(o) == first and first.assessment == "unverified"
    manifest = store.read(w["actor"], w["run"], first.manifest_artifact_id, w["token"])
    doc = json.loads(manifest.content)
    assert len(doc["files"]) == 1 and doc["files"][0]["target_path"] == "src/prototype.txt"
    content = store.read(w["actor"], w["run"], doc["files"][0]["artifact_id"], w["token"])
    assert content.content == b"deterministic prototype\n"
    assert content.evidence.assessment.status == "unverified"
    assert not (ws.path / "src/prototype.txt").exists()
    c.destroy(w["actor"], w["run"], w["token"])
    assert import_output(o) == first  # authenticated capsule, no resource recapture


def test_real_linux_reader_configuration_tamper_fail_closed(linux_output, monkeypatch):
    w, _, sb, c, _, _, _ = linux_output
    c.execute(w["actor"], w["run"], w["token"], "capture")
    with w["factory"]() as session:
        row = session.get(SandboxInstance, sb.id)
        original = c.driver.reader_info(row)
        call = c.driver.call
        for key, value in [
            ("Privileged", True),
            ("NetworkMode", "host"),
            ("ReadonlyRootfs", False),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("CapAdd", ["SYS_ADMIN"]),
            ("Memory", 0),
            ("Binds", ["/var/run/docker.sock:/var/run/docker.sock"]),
            ("SecurityOpt", []),
        ]:
            changed = copy.deepcopy(original)
            changed["HostConfig"][key] = value

            def altered(*args, changed=changed, **kwargs):
                if args == ("container", "inspect", c.driver.reader_name(row)):
                    from subprocess import CompletedProcess

                    return CompletedProcess(args, 0, json.dumps([changed]).encode(), b"")
                return call(*args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(c.driver, "call", altered)
                with pytest.raises(DomainError, match="output_reader_configuration_conflict"):
                    c.driver.reader_info(row)


def test_real_linux_toctou_capture_recheck(linux_output, monkeypatch):
    w, _, sb, c, _, _, profile = linux_output
    c.execute(w["actor"], w["run"], w["token"], "capture")
    with w["factory"]() as session:
        row = session.get(SandboxInstance, sb.id)
        original = c.driver._stopped
        calls = 0

        def changed(*args):
            nonlocal calls
            proof = original(*args)
            calls += 1
            if calls == 2:
                proof["finished_at"] = "changed"
            return proof

        with monkeypatch.context() as patch:
            patch.setattr(c.driver, "_stopped", changed)
            with pytest.raises(DomainError, match="output_changed_during_capture"):
                c.driver.export_output(row, profile)


@pytest.mark.parametrize("attack", ["symlink", "hardlink", "fifo", "oversize", "count", "empty"])
def test_real_linux_reader_rejects_hostile_files(linux_output, attack):
    # Trusted test-only helper injects fixtures, exits and is removed before capture.
    # It is not exposed by the production importer or a runtime profile.
    o = linux_output
    w, _, sb, c, _, _, profile = o
    c.execute(w["actor"], w["run"], w["token"], "capture")
    code = {
        "symlink": "import os;os.symlink('/etc/passwd','/output/escape')",
        "hardlink": "import os;os.link('/output/prototype.txt','/output/alias')",
        "fifo": "import os;os.mkfifo('/output/pipe')",
        "oversize": "open('/output/huge','wb').write(b'x'*(4*1024*1024+1))",
        "count": "from pathlib import Path;[(Path('/output')/str(i)).touch() for i in range(257)]",
        "empty": "from pathlib import Path;[p.unlink() for p in Path('/output').iterdir()]",
    }[attack]
    with w["factory"]() as session:
        row = session.get(SandboxInstance, sb.id)
        c.driver.call(
            "run",
            "--rm",
            "--pull=never",
            "--user",
            "10001:10001",
            "--read-only",
            "--network",
            "none",
            "--ipc",
            "none",
            "--cgroupns",
            "private",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "0.25",
            "--memory",
            "64m",
            "--memory-swap",
            "64m",
            "--pids-limit",
            "8",
            "--log-driver",
            "none",
            "--mount",
            "type=volume,src=" + c.driver.output_volume(row) + ",dst=/output,volume-nocopy",
            "--entrypoint",
            "/usr/local/bin/python",
            profile.image,
            "-I",
            "-B",
            "-c",
            code,
        )
    if attack == "empty":
        assert import_output(o).file_count == 0
    else:
        with pytest.raises(DomainError, match="output_capture_failed"):
            import_output(o)
        from hogwarts.models import ArtifactMetadata
        from sqlalchemy import func, select

        with w["factory"]() as session:
            assert session.scalar(select(func.count()).select_from(ArtifactMetadata)) == 0


def test_real_linux_foreign_volume_attachment_denied(linux_output):
    w, _, sb, c, _, _, profile = linux_output
    c.execute(w["actor"], w["run"], w["token"], "capture")
    with w["factory"]() as session:
        row = session.get(SandboxInstance, sb.id)
        name = c.driver.reader_name(row) + "-foreign-test"
        c.driver.call(
            "container",
            "create",
            "--pull=never",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            "type=volume,src="
            + c.driver.output_volume(row)
            + ",dst=/output,readonly,volume-nocopy",
            profile.image,
        )
        try:
            with pytest.raises(DomainError, match="output_has_unknown_mounts"):
                c.driver.export_output(row, profile)
        finally:
            c.driver.call("container", "rm", name)


@pytest.mark.parametrize("failure", ["deadline", "stream_limit"])
def test_real_linux_capture_transport_bounds(linux_output, monkeypatch, failure):
    import hogwarts.outputs.driver as module

    o = linux_output
    w, _, _, c, _, _, _ = o
    c.execute(w["actor"], w["run"], w["token"], "capture")
    if failure == "deadline":
        ticks = iter([0, 11])
        monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks, 11))
        expected = "output_capture_timeout"
    else:
        monkeypatch.setattr(module, "MAX_ARCHIVE", 17000)
        expected = "output_capture_limit"
    with pytest.raises(DomainError, match=expected):
        import_output(o)
    monkeypatch.undo()
    from hogwarts.models import OutputImport, WorkflowEvent
    from sqlalchemy import select

    with w["factory"]() as session:
        assert session.scalar(select(OutputImport)).state == "UNCERTAIN"
        assert any(
            e.payload.get("code") == expected
            for e in session.scalars(
                select(WorkflowEvent).where(WorkflowEvent.source == "secure_output_import")
            )
        )
