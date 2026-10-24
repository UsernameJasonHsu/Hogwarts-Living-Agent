"""Real local object IO/DB tests; M2.2 fake fixtures do not imply kernel verification."""

import hashlib
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import jsonschema
import pytest
from hogwarts.artifacts.contracts import (
    MAX_BYTES,
    ArtifactError,
    ArtifactUpload,
    Assessment,
    EvidenceDocument,
)
from hogwarts.artifacts.filesystem import LocalObjects
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.auth import Principal
from hogwarts.models import ArtifactMetadata, PrototypeRun, WorkflowEvent, now
from hogwarts.service import DomainError
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_sandboxes import real_sandbox as real_sandbox
from test_sandboxes import sandbox as sandbox
from test_workspaces import workspace as workspace

UPLOAD = ArtifactUpload(kind="report", media_type="application/json")
HUMAN = Principal("human:local-owner", "local-owner", "human")


@pytest.fixture
def artifacts(sandbox):
    w, workspace, _, controller = sandbox
    instance = controller.create(w["actor"], w["run"], workspace.id, w["token"], "verified")
    root = w["root"].parent / "private-artifacts"
    store = LocalArtifactStore(w["manager"], root)
    return w, workspace, instance, store


def put(a, content=b'{"verified":true,"tests_passed":999}', key="upload-1", upload=UPLOAD):
    w, workspace, instance, store = a
    return store.put(
        w["actor"], w["run"], workspace.id, instance.id, w["token"], key, upload, content
    )


def read(a, artifact_id):
    w, _, _, store = a
    return store.read(w["actor"], w["run"], artifact_id, w["token"])


def describe(a, artifact_id):
    w, _, _, store = a
    return store.describe(HUMAN, w["run"], artifact_id)


def object_path(a, artifact_id):
    return a[3].objects.path(artifact_id)


def tamper_file(path, data):
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.write_bytes(data)


def artifact_row(a):
    with a[0]["factory"]() as session:
        return session.scalar(select(ArtifactMetadata))


@pytest.mark.parametrize("content", [b"", b"hello\n", b"\0\xffbinary", b'{"verified":true}'])
def test_create_read_integrity_schema_unverified(artifacts, content):
    evidence = put(artifacts, content)
    identifier = evidence.artifact.artifact_id
    assert evidence.artifact.sha256 == hashlib.sha256(content).hexdigest()
    assert evidence.artifact.size_bytes == len(content)
    assert evidence.assessment.status == "unverified" and evidence.integrity == "valid"
    assert evidence.artifact.source == "agent_submission"
    assert evidence.artifact.producer_id == artifacts[0]["actor"].id
    assert evidence.artifact.execution.status == "unknown"
    result = read(artifacts, identifier)
    assert result.content == content and result.evidence == evidence
    assert EvidenceDocument.model_validate_json(evidence.model_dump_json()) == evidence
    schema = json.loads(Path("contracts/evidence.v1.schema.json").read_text())
    jsonschema.validate(evidence.model_dump(mode="json"), schema)
    assert describe(artifacts, identifier).integrity == "not_checked"
    if os.name == "posix":
        assert stat.S_IMODE(object_path(artifacts, identifier).stat().st_mode) == 0o400
    with pytest.raises(ValidationError):
        evidence.artifact.sha256 = "0" * 64


def test_committed_schema_matches_model():
    expected = EvidenceDocument.model_json_schema()
    actual = json.loads(Path("contracts/evidence.v1.schema.json").read_text())
    assert actual == expected


@pytest.mark.parametrize(
    "extra", ["verified", "source", "sha256", "path", "created_at", "assessment"]
)
def test_upload_cannot_set_trust_or_path(extra):
    with pytest.raises(ValidationError):
        ArtifactUpload.model_validate(
            {"kind": "report", "media_type": "application/json", extra: "forged"}
        )


def test_verified_assessment_requires_verifier_and_version_rejected(artifacts):
    with pytest.raises(ValidationError):
        Assessment(status="verified", reason="no_trusted_verifier")
    evidence = put(artifacts)
    changed = evidence.model_dump(mode="json")
    changed["schema_version"] = 2
    with pytest.raises(ValidationError):
        EvidenceDocument.model_validate_json(json.dumps(changed))


def test_same_key_replays_new_key_distinct_immutable_id(artifacts):
    first = put(artifacts)
    assert put(artifacts) == first
    other = put(artifacts, key="upload-2")
    assert first.artifact.artifact_id != other.artifact.artifact_id
    assert first.artifact.sha256 == other.artifact.sha256
    with artifacts[0]["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ArtifactMetadata)) == 2


@pytest.mark.parametrize("change", ["bytes", "kind", "media"])
def test_same_key_changed_request_conflicts(artifacts, change):
    original = put(artifacts)
    content, upload = b'{"verified":true,"tests_passed":999}', UPLOAD
    if change == "bytes":
        content = b"changed"
    elif change == "kind":
        upload = ArtifactUpload(kind="log", media_type="application/json")
    else:
        upload = ArtifactUpload(kind="report", media_type="text/plain")
    with pytest.raises(ArtifactError, match="artifact_request_conflict"):
        put(artifacts, content=content, upload=upload)
    assert read(artifacts, original.artifact.artifact_id).evidence == original


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_writes_separate_store_instances(artifacts, same_key):
    w, workspace, instance, store = artifacts
    stores = [LocalArtifactStore(w["manager"], store.objects.root) for _ in range(4)]

    def operation(index):
        return stores[index].put(
            w["actor"],
            w["run"],
            workspace.id,
            instance.id,
            w["token"],
            "shared" if same_key else f"key-{index}",
            UPLOAD,
            b"concurrent bytes",
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(operation, range(4)))
    assert len({r.artifact.artifact_id for r in results}) == (1 if same_key else 4)
    assert len(list(store.objects.objects.iterdir())) == (1 if same_key else 4)


def test_concurrent_conflicting_bodies_only_one_wins(artifacts):
    def operation(index):
        try:
            return put(artifacts, content=str(index).encode())
        except ArtifactError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(operation, range(3)))
    assert sum(isinstance(r, EvidenceDocument) for r in results) == 1
    assert results.count("artifact_request_conflict") == 2


@pytest.mark.parametrize(
    "kind", ["owner", "worker", "token", "workspace", "sandbox", "expired", "cancelled", "human"]
)
def test_create_authorization_denials(artifacts, kind):
    w, workspace, instance, store = artifacts
    actor, token, wid, sid = w["actor"], w["token"], workspace.id, instance.id
    if kind == "owner":
        actor = replace(actor, owner_id="other")
    elif kind == "worker":
        actor = replace(actor, id="worker:other")
    elif kind == "token":
        token = "wrong"
    elif kind == "workspace":
        wid = str(uuid4())
    elif kind == "sandbox":
        sid = str(uuid4())
    elif kind == "human":
        actor = HUMAN
    else:
        with w["factory"].begin() as session:
            run = session.get(PrototypeRun, w["run"])
            if kind == "expired":
                run.lease_expires_at = now() - timedelta(seconds=1)
            else:
                run.status = "CANCELLED"
    with pytest.raises(DomainError):
        store.put(actor, w["run"], wid, sid, token, "denied", UPLOAD, b"private")
    assert not list(store.objects.objects.iterdir())


def test_reads_are_run_scoped_and_owner_historical(artifacts, sandbox):
    w, _, _, store = artifacts
    result = put(artifacts)
    identifier = result.artifact.artifact_id
    with pytest.raises(DomainError):
        store.read(replace(HUMAN, owner_id="other"), w["run"], identifier)
    with pytest.raises(DomainError):
        store.read(w["actor"], w["run"], identifier, "wrong")
    with pytest.raises(DomainError):
        store.read(HUMAN, str(uuid4()), identifier)
    with w["factory"].begin() as session:
        session.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        read(artifacts, identifier)
    assert store.read(HUMAN, w["run"], identifier).content.startswith(b'{"verified"')

    sandbox[3].destroy(HUMAN, w["run"])
    w["manager"].cleanup(HUMAN, w["run"])
    assert not artifacts[1].path.exists()
    assert store.read(HUMAN, w["run"], identifier).content.startswith(b'{"verified"')


def test_real_other_run_cannot_read_or_bind(artifacts, api, seed):
    from conftest import headers, post

    w, workspace, instance, store = artifacts
    identifier = put(artifacts).artifact.artifact_id
    seed["prototype"].update(base_commit=w["sha"], allowed_paths=["src"])
    # Use the existing human cancellation API to release the original reservation.
    proposal_view = api.get(f"/api/v1/prototypes/{w['proposal']}", headers=headers()).json()
    cancelled = post(
        api, f"prototypes/{w['proposal']}/cancel", {"expected_version": proposal_view["version"]}
    )
    assert cancelled.status_code == 200
    proposal = post(api, "prototypes", seed).json()
    queued = post(api, f"prototypes/{proposal['id']}/start", {"expected_version": 1}).json()
    lease = post(
        api, f"runs/{queued['run_id']}/claim", {"expected_version": queued["version"]}, "worker"
    ).json()
    with pytest.raises(ArtifactError, match="artifact_not_found"):
        store.read(w["actor"], queued["run_id"], identifier, lease["lease_token"])
    with pytest.raises(ArtifactError, match="artifact_binding_mismatch"):
        store.put(
            w["actor"],
            queued["run_id"],
            workspace.id,
            instance.id,
            lease["lease_token"],
            "cross-run",
            UPLOAD,
            b"cross-run",
        )


@pytest.mark.parametrize(
    "identity",
    [
        "../outside",
        "/etc/passwd",
        "C:\\secret",
        "x:y",
        "A" * 36,
        "",
        "..",
        "00000000-0000-0000-0000-000000000000.blob",
    ],
)
def test_invalid_object_ids_never_read_paths(artifacts, identity, monkeypatch):
    w, _, _, store = artifacts

    def forbidden(*args):
        pytest.fail("unauthorized storage access")

    monkeypatch.setattr(store.objects, "read", forbidden)
    with pytest.raises(DomainError):
        store.read(HUMAN, w["run"], identity)


@pytest.mark.parametrize("root_kind", ["relative", "workspace", "repo", "ancestor"])
def test_unsafe_store_roots_rejected(artifacts, root_kind):
    w, _, _, _ = artifacts
    root = {
        "relative": Path("relative"),
        "workspace": w["root"] / "store",
        "repo": w["repo"] / "artifacts",
        "ancestor": w["root"].parent,
    }[root_kind]
    with pytest.raises(DomainError):
        LocalArtifactStore(w["manager"], root)


@pytest.mark.parametrize(
    "mutation", ["same_size", "different_size", "missing", "directory", "hardlink", "symlink"]
)
def test_tampering_links_missing_and_special_objects(artifacts, mutation):
    evidence = put(artifacts, content=b"original")
    identifier = evidence.artifact.artifact_id
    path = object_path(artifacts, identifier)
    outside = path.parent.parent / "outside-sentinel"
    outside.write_bytes(b"sentinel")
    if mutation == "same_size":
        tamper_file(path, b"tampered")
    elif mutation == "different_size":
        tamper_file(path, b"longer tampering")
    elif mutation == "hardlink":
        os.link(path, outside.parent / "second-link")
    else:
        path.chmod(stat.S_IWRITE | stat.S_IREAD)
        path.unlink()
        if mutation == "directory":
            path.mkdir()
        elif mutation == "symlink":
            try:
                path.symlink_to(outside)
            except OSError:
                pytest.skip("native symlink unavailable; Linux verifies this case")
    try:
        with pytest.raises(DomainError):
            read(artifacts, identifier)
        assert artifact_row(artifacts).state == "CORRUPT"
        view = describe(artifacts, identifier)
        assert view.assessment.status == "failed" and view.integrity == "failed"
        assert outside.read_bytes() == b"sentinel"
        with pytest.raises(DomainError):
            put(artifacts, content=b"original")
    finally:
        if path.is_symlink():
            path.unlink()


def test_metadata_tampering_fails_closed(artifacts):
    evidence = put(artifacts)
    with artifacts[0]["factory"].begin() as session:
        row = session.get(ArtifactMetadata, evidence.artifact.artifact_id)
        row.evidence = {**row.evidence, "source": "trusted_verifier"}
    with pytest.raises(ArtifactError, match="artifact_metadata_corrupt"):
        read(artifacts, evidence.artifact.artifact_id)
    assert artifact_row(artifacts).state == "CORRUPT"


@pytest.mark.parametrize("stage", ["publish", "fsync", "partial"])
def test_storage_failure_is_audited_and_never_overwritten(artifacts, stage, monkeypatch):
    w, _, _, store = artifacts
    if stage == "fsync":

        def fail(*args):
            raise OSError("private filesystem details")

        monkeypatch.setattr("hogwarts.artifacts.filesystem.os.fsync", fail)
    else:

        def fail(identifier, content, checksum):
            if stage == "partial":
                store.objects.path(identifier).write_bytes(content[:2])
            raise OSError("private filesystem details")

        monkeypatch.setattr(store.objects, "publish", fail)
    with pytest.raises(ArtifactError, match="artifact_storage_failed"):
        put(artifacts)
    row = artifact_row(artifacts)
    assert row.state == "FAILED"
    assert describe(artifacts, row.id).assessment.status == "failed"
    with pytest.raises(ArtifactError, match="artifact_not_publishable"):
        put(artifacts)
    with w["factory"]() as session:
        events = list(
            session.scalars(select(WorkflowEvent).where(WorkflowEvent.source == "artifact_store"))
        )
        assert any(e.event_type == "artifact.failed" for e in events)
        assert "private filesystem details" not in str([e.payload for e in events])
        assert w["token"] not in str([e.payload for e in events])


def test_database_final_commit_failure_recovers_exact_durable_bytes(artifacts):
    triggered = False

    def before_commit(session):
        nonlocal triggered
        if not triggered and any(
            isinstance(row, ArtifactMetadata) and row.state == "READY"
            for row in session.identity_map.values()
        ):
            triggered = True
            raise OperationalError("commit", None, Exception("private DB details"))

    event.listen(Session, "before_commit", before_commit)
    try:
        with pytest.raises(ArtifactError, match="artifact_database_failed"):
            put(artifacts)
    finally:
        event.remove(Session, "before_commit", before_commit)
    assert triggered, "commit failure injection must actually fire"
    row = artifact_row(artifacts)
    assert row.state == "PENDING" and object_path(artifacts, row.id).exists()
    original = object_path(artifacts, row.id).read_bytes()
    evidence = put(artifacts)
    assert evidence.artifact.artifact_id == row.id and evidence.storage_state == "READY"
    assert read(artifacts, row.id).content == original


def test_database_before_intent_no_file_effect(artifacts, engine):
    triggered = False

    def before_cursor(connection, cursor, statement, parameters, context, many):
        nonlocal triggered
        if not triggered and statement.startswith("INSERT INTO artifact_metadata"):
            triggered = True
            raise OperationalError("insert", None, Exception("private DB details"))

    event.listen(engine, "before_cursor_execute", before_cursor)
    try:
        with pytest.raises(ArtifactError, match="artifact_database_failed"):
            put(artifacts)
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor)
    assert not list(artifacts[3].objects.objects.iterdir())
    assert artifact_row(artifacts) is None


def test_db_and_audit_unavailable_reported_honestly(artifacts, monkeypatch):
    class Unavailable:
        def begin(self):
            raise OperationalError("connect", None, Exception("credentials must not leak"))

    monkeypatch.setattr(artifacts[3], "factory", Unavailable())
    with pytest.raises(ArtifactError, match="artifact_database_and_audit_unavailable"):
        put(artifacts)
    assert not list(artifacts[3].objects.objects.iterdir())


def test_read_io_failure_redacted(artifacts, monkeypatch):
    evidence = put(artifacts)

    def failure(*args):
        raise OSError("host credentials private")

    monkeypatch.setattr(artifacts[3].objects, "read", failure)
    with pytest.raises(ArtifactError, match="artifact_storage_failed"):
        read(artifacts, evidence.artifact.artifact_id)


def test_size_and_mutable_content_denied(artifacts):
    with pytest.raises(ArtifactError, match="invalid_artifact_bytes"):
        put(artifacts, content=bytearray(b"mutable"))
    with pytest.raises(ArtifactError, match="invalid_artifact_bytes"):
        put(artifacts, content=b"x" * (MAX_BYTES + 1))


def test_raw_filesystem_exclusive_create_never_overwrites(tmp_path):
    objects = LocalObjects(tmp_path / "objects-private", ())
    identifier, content = str(uuid4()), b"first bytes"
    checksum = hashlib.sha256(content).hexdigest()
    objects.publish(identifier, content, checksum)
    with pytest.raises(DomainError):
        objects.publish(identifier, b"new bytes", hashlib.sha256(b"new bytes").hexdigest())
    assert objects.read(identifier, checksum, len(content)) == content


def test_real_controller_result_stays_unverified(real_sandbox):
    w, workspace, _, build = real_sandbox
    controller = build("verify")
    result = controller.execute(w["actor"], w["run"], w["token"], "artifact-fixture")
    store = LocalArtifactStore(w["manager"], w["root"].parent / "real-artifacts")
    evidence = store.put(
        w["actor"],
        w["run"],
        workspace.id,
        result.id,
        w["token"],
        "controller-report",
        UPLOAD,
        b'{"verified":true}',
    )
    assert evidence.artifact.execution.status == "succeeded"
    assert evidence.artifact.execution.started_at and evidence.artifact.execution.finished_at
    assert evidence.assessment.status == "unverified"


def test_json_schema_rejects_forged_verified_without_ref(artifacts):
    evidence = put(artifacts).model_dump(mode="json")
    evidence["assessment"] = {"status": "verified", "reason": "no_trusted_verifier"}
    schema = json.loads(Path("contracts/evidence.v1.schema.json").read_text())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(evidence, schema)


def test_schema_version_persisted_and_tamper_detected(artifacts):
    identifier = put(artifacts).artifact.artifact_id
    assert artifact_row(artifacts).schema_version == 1
    with artifacts[0]["factory"].begin() as session:
        session.get(ArtifactMetadata, identifier).schema_version = 2
    with pytest.raises(ArtifactError, match="artifact_metadata_corrupt"):
        read(artifacts, identifier)


@pytest.mark.parametrize("target", ["root", "objects"])
def test_symlink_directory_refused_without_outside_writes(tmp_path, target):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("untouched")
    root = tmp_path / "unsafe-store"
    link = root
    if target == "objects":
        root.mkdir(mode=0o700)
        link = root / "objects"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("native directory symlink unavailable; Linux verifies this case")
    try:
        with pytest.raises(DomainError):
            LocalObjects(root, ())
        assert sorted(p.name for p in outside.iterdir()) == ["sentinel"]
        assert sentinel.read_text() == "untouched"
    finally:
        link.unlink()


def test_fifo_object_refused_without_blocking(artifacts):
    if os.name != "posix":
        pytest.skip("POSIX FIFO unavailable on Windows; Linux verifies this case")
    identifier = put(artifacts).artifact.artifact_id
    path = object_path(artifacts, identifier)
    path.chmod(0o600)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(DomainError):
        read(artifacts, identifier)
    assert artifact_row(artifacts).state == "CORRUPT"
    path.unlink()


def test_lease_expiry_during_read_returns_no_bytes(artifacts, monkeypatch):
    identifier = put(artifacts).artifact.artifact_id
    original = artifacts[3].objects.read

    def expire(*args):
        data = original(*args)
        monkeypatch.setattr(
            "hogwarts.workspaces.manager.now", lambda: now() + timedelta(minutes=10)
        )
        return data

    monkeypatch.setattr(artifacts[3].objects, "read", expire)
    with pytest.raises(DomainError, match="inactive_workspace_lease"):
        read(artifacts, identifier)


def test_pending_partial_bytes_are_not_adopted_or_overwritten(artifacts, monkeypatch):
    store = artifacts[3]

    def crash(identifier, content, checksum):
        store.objects.path(identifier).write_bytes(content[:3])
        raise OperationalError(
            "interrupted transaction", None, Exception("injected database outage")
        )

    with monkeypatch.context() as patch:
        patch.setattr(store.objects, "publish", crash)
        with pytest.raises(ArtifactError, match="artifact_database_failed"):
            put(artifacts)
    row = artifact_row(artifacts)
    assert row.state == "PENDING"
    original = store.objects.path(row.id).read_bytes()
    with pytest.raises(ArtifactError, match="artifact_integrity_failed"):
        put(artifacts)
    assert artifact_row(artifacts).state == "CORRUPT"
    assert store.objects.path(row.id).read_bytes() == original
