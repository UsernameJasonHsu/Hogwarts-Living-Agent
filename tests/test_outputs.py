"""Importer contracts use real local IO/SQL and an explicitly fake capture driver."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.models import (
    ArtifactMetadata,
    OutputImport,
    PrototypeRun,
    SandboxInstance,
    WorkflowEvent,
    now,
)
from hogwarts.outputs.contracts import OutputError, OutputGrant
from hogwarts.outputs.driver import OutputController
from hogwarts.outputs.importer import LocalOutputImporter
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.service import DomainError
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_output_contracts import archive as make_archive
from test_sandboxes import IMAGE, FakeDriver
from test_workspaces import workspace as workspace


def archive(entries):
    return make_archive([(name, content, b"0") for name, content in entries])


class CaptureDriver(FakeDriver):
    reader_image = "sha256:" + "b" * 64

    def __init__(self):
        super().__init__()
        self.data = archive([("prototype.txt", b"hello")])
        self.captures = 0
        self.broken = False

    def export_output(self, row, profile):
        self.captures += 1
        if self.broken:
            raise OutputError(409, "output_not_stable")
        return self.data, {
            "container_id": row.container_id,
            "invocation_id": row.invocation_id,
            "profile_hash": row.request_hash,
            "sandbox_input_sha256": row.snapshot_sha256,
            "reader_image": self.reader_image,
            "finished_at": "fixed-finish",
        }


@pytest.fixture
def outputs(workspace):
    w = workspace
    info = w["manager"].create(w["actor"], w["run"], w["token"])
    driver = CaptureDriver()
    controller = OutputController(w["manager"], driver, (RuntimeProfile("verified", IMAGE),))
    sandbox = controller.create(w["actor"], w["run"], info.id, w["token"], "verified")
    controller.start(w["actor"], w["run"], w["token"])
    controller.execute(w["actor"], w["run"], w["token"], "execute-1")
    store = LocalArtifactStore(w["manager"], w["root"].parent / "artifacts")
    importer = LocalOutputImporter(
        controller, store, w["root"].parent / "capsules", (OutputGrant("verified", "src"),)
    )
    return w, info, sandbox, controller, store, importer


def run(o, **changes):
    w, ws, sb, controller, _, importer = o
    args = dict(
        controller=controller,
        actor=w["actor"],
        run_id=w["run"],
        workspace_id=ws.id,
        sandbox_id=sb.id,
        lease_token=w["token"],
    )
    args.update(changes)
    return importer.import_output(**args)


def row(o):
    with o[0]["factory"]() as session:
        return session.scalar(select(OutputImport))


def count(o):
    with o[0]["factory"]() as session:
        return session.scalar(select(func.count()).select_from(ArtifactMetadata))


@pytest.mark.parametrize("empty", [False, True])
def test_normal_empty_fixed_snapshot_unverified_and_restart(outputs, empty):
    o = outputs
    w, ws, sb, c, store, importer = o
    if empty:
        c.driver.data = archive([])
    first = run(o)
    assert first.state == "IMPORTED" and first.assessment == "unverified"
    assert first.file_count == (0 if empty else 1)
    c.driver.data = archive([("later.txt", b"must not recapture")])
    restarted = LocalOutputImporter(
        c, store, importer.vault.objects.root, (OutputGrant("verified", "src"),)
    )
    assert restarted.import_output(c, w["actor"], w["run"], ws.id, sb.id, w["token"]) == first
    assert c.driver.captures == 1 and count(o) == (2 if empty else 3)
    manifest = store.read(w["actor"], w["run"], first.manifest_artifact_id, w["token"])
    doc = json.loads(manifest.content)
    assert doc["assessment"] == "unverified" and manifest.evidence.assessment.status == "unverified"
    if not empty:
        file = doc["files"][0]
        assert file["target_path"] == "src/prototype.txt"
        assert store.read(w["actor"], w["run"], file["artifact_id"], w["token"]).content == b"hello"
    assert not (ws.path / "src/prototype.txt").exists()
    with w["factory"]() as session:
        events = list(
            session.scalars(
                select(WorkflowEvent).where(WorkflowEvent.source == "secure_output_import")
            )
        )
        assert any(e.event_type == "output_import.imported" for e in events)
        assert w["token"] not in str([e.payload for e in events])
        assert row(o).seal_key not in str([e.payload for e in events])


@pytest.mark.parametrize(
    "kind",
    [
        "controller",
        "run",
        "workspace",
        "sandbox",
        "token",
        "owner",
        "worker",
        "human",
        "expired",
        "cancelled",
        "running",
        "failed",
        "grant",
        "allowed",
    ],
)
def test_authorization_before_capture(outputs, kind):
    o = outputs
    w, ws, sb, c, _, _ = o
    args = {}
    if kind == "controller":
        args["controller"] = object()
    if kind in {"run", "workspace", "sandbox"}:
        args[kind + "_id"] = str(uuid4())
    if kind == "token":
        args["lease_token"] = "invalid"
    if kind == "owner":
        args["actor"] = replace(w["actor"], owner_id="other")
    if kind == "worker":
        args["actor"] = replace(w["actor"], id="worker:other")
    if kind == "human":
        args["actor"] = replace(w["actor"], role="human")
    if kind in {"expired", "cancelled", "running", "failed"}:
        with w["factory"].begin() as session:
            if kind in {"expired", "cancelled"}:
                target = session.get(PrototypeRun, w["run"])
                if kind == "expired":
                    target.lease_expires_at = now() - timedelta(seconds=1)
                else:
                    target.status = "CANCELLED"
            else:
                target = session.get(SandboxInstance, sb.id)
                target.state = kind.upper()
    if kind == "grant":
        w["manager"].grants.clear()
    if kind == "allowed":
        w["manager"].grants[w["grant"].project_id] = replace(w["grant"], allowed_paths=("tests",))
    with pytest.raises(DomainError):
        run(o, **args)
    assert c.driver.captures == 0 and count(o) == 0


@pytest.mark.parametrize(
    "name", ["../outside", "/absolute", ".git/config", "A/../b", "\\bad", "secret.txt/../../x"]
)
def test_archive_preflight_publishes_nothing(outputs, name):
    outputs[3].driver.data = archive([(name, b"bad")])
    with pytest.raises(DomainError):
        run(outputs)
    assert count(outputs) == 0 and row(outputs).state == "UNCERTAIN"
    with pytest.raises(DomainError):
        run(outputs)
    assert outputs[3].driver.captures == 1


def test_capture_failure_is_durable_and_never_recaptured(outputs):
    outputs[3].driver.broken = True
    with pytest.raises(OutputError, match="output_not_stable"):
        run(outputs)
    assert row(outputs).state == "UNCERTAIN" and count(outputs) == 0
    outputs[3].driver.broken = False
    with pytest.raises(OutputError):
        run(outputs)
    assert outputs[3].driver.captures == 1


def test_concurrent_import_single_capture_and_identical_artifacts(outputs):
    def operation(_):
        try:
            return run(outputs)
        except OutputError as error:
            assert error.code == "output_capture_busy_or_uncertain"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(operation, range(4)))
    final = run(outputs)
    assert all(r is None or r == final for r in results)
    assert outputs[3].driver.captures == 1 and count(outputs) == 3


@pytest.mark.parametrize("failure", ["storage", "database", "final_commit"])
def test_publication_failure_retries_only_sealed_snapshot(outputs, monkeypatch, failure):
    o = outputs
    original = o[4].put
    fired = False

    def broken(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            if failure == "storage":
                raise OSError("injected")
            if failure == "database":
                raise OperationalError("injected", {}, Exception())
        return original(*args, **kwargs)

    if failure == "final_commit":

        def fail(session):
            nonlocal fired
            if not fired and any(
                isinstance(x, OutputImport) and x.state == "IMPORTED" for x in session.dirty
            ):
                fired = True
                raise OperationalError("injected", {}, Exception())

        event.listen(Session, "before_commit", fail)
    else:
        monkeypatch.setattr(o[4], "put", broken)
    try:
        with pytest.raises(OutputError):
            run(o)
    finally:
        if failure == "final_commit":
            event.remove(Session, "before_commit", fail)
    monkeypatch.setattr(o[4], "put", original)
    assert row(o).state == "IMPORTING"
    assert run(o).state == "IMPORTED" and o[3].driver.captures == 1 and count(o) == 3


def test_snapshot_commit_failure_adopts_authenticated_capsule(outputs):
    fired = False

    def fail(session):
        nonlocal fired
        if not fired and any(
            isinstance(x, OutputImport) and x.state == "SNAPSHOT_READY" for x in session.dirty
        ):
            fired = True
            raise OperationalError("injected", {}, Exception())

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(OutputError):
            run(outputs)
    finally:
        event.remove(Session, "before_commit", fail)
    assert row(outputs).state == "SNAPSHOTTING"
    assert run(outputs).state == "IMPORTED" and outputs[3].driver.captures == 1


@pytest.mark.parametrize("kind", ["bytes", "hardlink", "symlink", "missing"])
def test_capsule_tamper_fails_closed(outputs, kind):
    if kind == "symlink" and os.name == "nt":
        pytest.skip("Windows symlink privilege absent; Linux executes")
    run(outputs)
    p = outputs[5].vault.objects.path(row(outputs).id)
    p.chmod(0o600)
    if kind == "bytes":
        with p.open("r+b") as stream:
            original = stream.read(1)
            stream.seek(0)
            stream.write(bytes([original[0] ^ 1]))
    elif kind == "hardlink":
        os.link(p, p.with_suffix(".extra"))
    elif kind == "symlink":
        data = p.read_bytes()
        p.unlink()
        other = p.with_suffix(".other")
        other.write_bytes(data)
        p.symlink_to(other)
    else:
        p.unlink()
    with pytest.raises(DomainError):
        run(outputs)
    assert outputs[3].driver.captures == 1


def test_no_host_path_or_archive_or_shell_parameters(outputs):
    for kw in ["host_path", "archive", "command", "docker_args"]:
        with pytest.raises(TypeError):
            run(outputs, **{kw: "/etc/passwd"})
    assert outputs[3].driver.captures == 0


@pytest.mark.parametrize("kind", ["expired", "grant", "workspace", "lease", "profile"])
def test_replay_rechecks_authorization_and_configuration(outputs, kind):
    o = outputs
    run(o)
    w, ws, sb, c, _, _ = o
    if kind in {"expired", "lease", "workspace"}:
        with w["factory"].begin() as session:
            if kind == "workspace":
                from hogwarts.models import GitWorkspace

                session.get(GitWorkspace, ws.id).state = "CLEANED"
            else:
                target = session.get(PrototypeRun, w["run"])
                if kind == "expired":
                    target.lease_expires_at = now() - timedelta(seconds=1)
                else:
                    target.lease_token = "changed"
    elif kind == "profile":
        c.profiles = {"verified": RuntimeProfile("verified", IMAGE, cpus=1)}
    else:
        w["manager"].grants.clear()
    with pytest.raises(DomainError):
        run(o)
    assert c.driver.captures == 1 and count(o) == 3


def test_preexisting_uncertain_intent_never_captures(outputs):
    # Simulate controller death after intent commit, before first filesystem write.
    o = outputs
    w, ws, sb, _, _, i = o
    with w["factory"].begin() as session:
        *_, fingerprint = i._binding(session, w["actor"], w["run"], ws.id, sb.id, w["token"])
        session.add(
            OutputImport(
                run_id=w["run"],
                workspace_id=ws.id,
                sandbox_id=sb.id,
                owner_id=w["actor"].owner_id,
                producer_id=w["actor"].id,
                lease_hash="unused",
                request_hash=fingerprint,
                seal_key="a" * 64,
                state="SNAPSHOTTING",
            )
        )
    with pytest.raises(OutputError, match="output_capture_busy_or_uncertain"):
        run(o)
    assert o[3].driver.captures == 0 and count(o) == 0


def test_capsule_publication_failure_is_terminal(outputs, monkeypatch):
    def fail(*args):
        raise OSError("injected capsule IO failure")

    monkeypatch.setattr(outputs[5].vault.objects, "publish", fail)
    with pytest.raises(OutputError):
        run(outputs)
    assert row(outputs).state == "UNCERTAIN" and count(outputs) == 0
    with pytest.raises(OutputError):
        run(outputs)
    assert outputs[3].driver.captures == 1


def test_unavailable_database_does_not_claim_audit_success(outputs, monkeypatch):
    class BrokenFactory:
        def begin(self):
            raise OperationalError("injected", {}, Exception())

    monkeypatch.setattr(outputs[5], "factory", BrokenFactory())
    with pytest.raises(OutputError, match="output_database_and_audit_unavailable"):
        run(outputs)
    assert outputs[3].driver.captures == 0


def test_imported_artifact_tampering_is_detected_on_replay(outputs):
    first = run(outputs)
    p = outputs[4].objects.path(first.manifest_artifact_id)
    p.chmod(0o600)
    with p.open("r+b") as stream:
        stream.write(b"changed")
    with pytest.raises(DomainError):
        run(outputs)
    assert outputs[3].driver.captures == 1


@pytest.mark.parametrize("target", ["workspace", "sandbox"])
def test_actual_foreign_run_binding_denied(outputs, target):
    from hogwarts.models import GitWorkspace, Proposal

    o = outputs
    w, ws, sb, _, _, _ = o
    with w["factory"].begin() as session:
        original = session.get(Proposal, w["proposal"])
        values = {
            c.name: getattr(original, c.name) for c in Proposal.__table__.columns if c.name != "id"
        }
        other = Proposal(**values)
        session.add(other)
        session.flush()
        foreign = PrototypeRun(proposal_id=other.id)
        session.add(foreign)
        session.flush()
        model, identifier = (
            (GitWorkspace, ws.id) if target == "workspace" else (SandboxInstance, sb.id)
        )
        session.get(model, identifier).run_id = foreign.id
    with pytest.raises(OutputError, match="artifact_binding_mismatch"):
        run(o)
    assert o[3].driver.captures == 0 and count(o) == 0
