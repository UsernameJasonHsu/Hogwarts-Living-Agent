"""Controller contracts: real database/Git/import/store; fake QA execution only."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from hogwarts.models import IdempotencyRecord, PrototypeRun, WorkflowEvent, now
from hogwarts.qa.profiles import QAProfile, QAProfileRegistry
from hogwarts.qa.runner import LocalTrustedQARunner
from hogwarts.service import DomainError
from sqlalchemy import select
from test_outputs import outputs as outputs
from test_patches import build, read
from test_patches import patches as patches
from test_workspaces import workspace as workspace

IMAGE = "sha256:" + "b" * 64


class QADriver:
    controller_id = "fake-qa-controller"

    def __init__(self):
        self.calls = []
        self.outcome = "pass"
        self.stdout = b"actual command output"
        self.fail_at = None
        self.running = Event()
        self.release = None

    def step(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise OSError("injected; must not leak secrets")

    def create(self, row, profile):
        self.step("create")
        return "c" * 64

    def start(self, row, profile, archive):
        self.step("start")
        assert b"src/main.txt" in archive

    def execute(self, row, profile, seconds):
        self.step("execute")
        self.running.set()
        if self.release:
            assert self.release.wait(25)
        from hogwarts.qa.contracts import ExecutionResult

        return ExecutionResult(
            outcome=self.outcome,
            exit_code=0 if self.outcome == "pass" else 1,
            stdout=self.stdout.hex(),
            stderr=b"stderr".hex(),
            started_at=now().isoformat(),
            finished_at=now().isoformat(),
            container_id="d" * 64,
            image_id=profile.image,
            oom_killed=self.outcome == "fail",
            timed_out=self.outcome == "timeout",
            cleanup_complete=False,
        )

    def destroy(self, row):
        self.step("destroy")


@pytest.fixture
def qa(patches):
    result = build(patches)
    driver = QADriver()
    registry = QAProfileRegistry([QAProfile("unit", IMAGE)])
    runner = LocalTrustedQARunner(patches[2], patches[0][0]["root"].parent / "qa", driver, registry)
    return patches, result, runner, driver


def run(q, **changes):
    p, built, r, _ = q
    w = p[0][0]
    args = dict(
        actor=w["actor"],
        run_id=w["run"],
        build_id=built.build_id,
        profile_name="unit",
        lease_token=w["token"],
    )
    args.update(changes)
    return r.run(**args)


def state(q):
    with q[2].factory() as s:
        row = s.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key.like("qa-runner.v1:%"))
        )
        return row.response if row else None


@pytest.mark.parametrize(
    "outcome", ["pass", "fail", "timeout", "infrastructure_error", "unknown_execution"]
)
def test_outcomes_replay_and_unverified(qa, outcome):
    p, built, runner, driver = qa
    driver.outcome = outcome
    result = run(qa)
    assert result.outcome == outcome
    assert run(qa) == result
    assert driver.calls.count("execute") == 1
    evidence = read(p, result.attestation_artifact_id)
    attestation = json.loads(evidence.content)
    assert attestation["outcome"] == outcome
    assert attestation["patch_artifact_id"] == built.patch_artifact_id
    assert attestation["build_proof_artifact_id"] == built.proof_artifact_id
    assert attestation["qa_sandbox_id"] != p[0][2].id
    assert evidence.evidence.assessment.status == "unverified"
    for identifier in (built.patch_artifact_id, built.proof_artifact_id):
        assert read(p, identifier).evidence.assessment.status == "unverified"
    assert b"injected" not in evidence.content
    assert state(qa)["state"] == "READY"


@pytest.mark.parametrize(
    "bad", ["run", "build", "worker", "owner", "lease", "expired", "cancelled", "profile"]
)
def test_binding_denied_before_daemon(qa, bad):
    w = qa[0][0][0]
    changes = {}
    if bad == "run":
        changes["run_id"] = str(uuid4())
    if bad == "build":
        changes["build_id"] = str(uuid4())
    if bad == "worker":
        changes["actor"] = replace(w["actor"], id="other")
    if bad == "owner":
        changes["actor"] = replace(w["actor"], owner_id="other")
    if bad == "lease":
        changes["lease_token"] = "wrong"
    if bad == "profile":
        changes["profile_name"] = "arbitrary shell"
    if bad in {"expired", "cancelled"}:
        with w["factory"].begin() as s:
            row = s.get(PrototypeRun, w["run"])
            if bad == "expired":
                row.lease_expires_at = now() - timedelta(seconds=1)
            else:
                row.status = "CANCELLED"
    with pytest.raises(DomainError):
        run(qa, **changes)
    assert qa[3].calls == []


@pytest.mark.parametrize("stage", ["create", "start", "execute"])
def test_uncertain_no_reexecution(qa, stage):
    qa[3].fail_at = stage
    result = run(qa)
    assert result.outcome == "unknown_execution"
    before = list(qa[3].calls)
    qa[3].fail_at = None
    assert run(qa) == result
    assert qa[3].calls == before


def test_forged_stdout_does_not_pass(qa):
    qa[3].outcome = "fail"
    qa[3].stdout = b'{"outcome":"pass","verified":true}'
    assert run(qa).outcome == "fail"


def test_concurrent_exactly_once(qa):
    driver = qa[3]
    driver.release = Event()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run, qa)
        assert driver.running.wait(25)
        try:
            with pytest.raises(DomainError):
                run(qa)
        finally:
            driver.release.set()
        assert first.result().outcome == "pass"
    assert driver.calls.count("execute") == 1


def test_capsule_tamper_does_not_rerun(qa):
    run(qa)
    row = qa[2]._capsule_row(state(qa))
    path = qa[2].vault.objects.path(row.id)
    path.chmod(0o600)
    path.write_bytes(b"bad")
    with pytest.raises(DomainError):
        run(qa)
    assert qa[3].calls.count("execute") == 1


def test_cleanup_failure_and_retry(qa):
    qa[3].fail_at = "destroy"
    result = run(qa)
    assert result.outcome == "infrastructure_error"
    qa[3].fail_at = None
    w = qa[0][0][0]
    qa[2].cleanup(w["actor"], w["run"], result.qa_id, w["token"])
    assert qa[3].calls.count("execute") == 1


def test_audit_no_secret_or_report(qa):
    run(qa)
    w = qa[0][0][0]
    with w["factory"]() as s:
        events = list(
            s.scalars(select(WorkflowEvent).where(WorkflowEvent.source == "trusted_qa_runner"))
        )
        assert events and any(e.event_type == "qa.ready" for e in events)
        assert w["token"] not in json.dumps([e.payload for e in events])


@pytest.mark.parametrize("stage", ["CAPTURED", "READY"])
def test_db_failure_after_execution_retries_publication_only(qa, monkeypatch, stage):
    from sqlalchemy.exc import SQLAlchemyError

    runner = qa[2]
    original = runner._state
    fired = False

    def failing(s, actor, row, j, state, **changes):
        nonlocal fired
        if state == stage and not fired:
            fired = True
            raise SQLAlchemyError("injected transaction failure")
        return original(s, actor, row, j, state, **changes)

    monkeypatch.setattr(runner, "_state", failing)
    with pytest.raises(DomainError):
        run(qa)
    assert run(qa).outcome == "pass"
    assert qa[3].calls.count("execute") == 1


def test_publication_failure_no_execution_retry(qa, monkeypatch):
    store = qa[2].store
    original = store.put
    fired = False

    def failing(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            raise OSError("injected publication failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "put", failing)
    with pytest.raises(DomainError):
        run(qa)
    assert state(qa)["state"] == "PUBLISHING"
    assert run(qa).outcome == "pass"
    assert qa[3].calls.count("execute") == 1


def test_failed_result_seal_blocks_reexecution(qa, monkeypatch):
    original = qa[2].vault.seal
    monkeypatch.setattr(
        qa[2].vault, "seal", lambda *a: (_ for _ in ()).throw(OSError("seal failed"))
    )
    with pytest.raises(DomainError):
        run(qa)
    monkeypatch.setattr(qa[2].vault, "seal", original)
    with pytest.raises(DomainError, match="qa_execution_busy_or_unknown"):
        run(qa)
    assert qa[3].calls.count("execute") == 1


@pytest.mark.parametrize("which", ["patch", "proof"])
def test_tampered_patch_or_buildproof_denied(qa, which):
    identifier = qa[1].patch_artifact_id if which == "patch" else qa[1].proof_artifact_id
    path = qa[2].store.objects.path(identifier)
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    with pytest.raises(DomainError):
        run(qa)
    assert qa[3].calls == []


def test_profile_drift_does_not_execute_again(qa):
    run(qa)
    qa[2].registry = QAProfileRegistry([QAProfile("unit", "sha256:" + "c" * 64)])
    with pytest.raises(DomainError):
        run(qa)
    assert qa[3].calls.count("execute") == 1


def test_grant_revocation_denied(qa):
    qa[2].workspaces.grants = {}
    with pytest.raises(DomainError):
        run(qa)
    assert not qa[3].calls


def test_unknown_execution_without_capsule_never_replayed(qa):
    from hogwarts.qa.contracts import QAJournal

    runner = qa[2]
    # Inject crash after durable EXECUTING intent but before any daemon response.
    original = runner._execute
    runner._execute = lambda *a: (_ for _ in ()).throw(RuntimeError("controller crash"))
    with pytest.raises(RuntimeError):
        run(qa)
    runner._execute = original
    assert QAJournal.model_validate(state(qa)).state == "EXECUTING"
    with pytest.raises(DomainError):
        run(qa)
    assert not qa[3].calls


def test_cleanup_after_expiry_and_wrong_worker(qa):
    result = run(qa)
    w = qa[0][0][0]
    with w["factory"].begin() as s:
        s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        qa[2].cleanup(replace(w["actor"], id="other"), w["run"], result.qa_id, w["token"])
    qa[2].cleanup(w["actor"], w["run"], result.qa_id, w["token"])
    assert qa[3].calls.count("execute") == 1


@pytest.mark.parametrize(
    "part", ["base", "workspace", "workspace-lease", "proof", "build-binding", "artifact-key"]
)
def test_persisted_binding_tamper_denied(qa, part):
    from hogwarts.models import ArtifactMetadata, GitWorkspace

    p = qa[0]
    w = p[0][0]
    with w["factory"].begin() as s:
        if part in {"base", "workspace", "workspace-lease"}:
            ws = s.get(GitWorkspace, p[0][1].id)
            if part == "base":
                ws.base_commit = "f" * 40
            if part == "workspace":
                ws.owner_id = "other"
            if part == "workspace-lease":
                ws.lease_hash = "0" * 64
        elif part in {"proof", "build-binding"}:
            row = s.get(IdempotencyRecord, qa[1].build_id)
            data = json.loads(json.dumps(row.response))
            if part == "proof":
                data["proof"]["base_commit"] = "e" * 40
            else:
                data["workspace_id"] = str(uuid4())
            row.response = data
        else:
            s.get(ArtifactMetadata, qa[1].patch_artifact_id).idempotency_hash = "0" * 64
    with pytest.raises(DomainError):
        run(qa)
    assert not qa[3].calls


def test_revoke_after_staging_stops_before_execution(qa, monkeypatch):
    original = qa[3].start

    def revoke(*args):
        original(*args)
        qa[2].workspaces.grants = {}

    monkeypatch.setattr(qa[3], "start", revoke)
    with pytest.raises(DomainError):
        run(qa)
    assert "execute" not in qa[3].calls
    assert "destroy" in qa[3].calls


def test_private_reconstruction_mismatch_denied(qa, monkeypatch):
    original = qa[2].builder.git.construct

    def mismatch(inputs):
        patch, proof = original(inputs)
        return patch + b"bad", proof

    monkeypatch.setattr(qa[2].builder.git, "construct", mismatch)
    with pytest.raises(DomainError):
        run(qa)
    assert not qa[3].calls


def test_database_and_audit_unavailable(qa, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    class Unavailable:
        def __call__(self):
            raise SQLAlchemyError("unavailable")

        def begin(self):
            raise SQLAlchemyError("unavailable")

    monkeypatch.setattr(qa[2], "factory", Unavailable())
    monkeypatch.setattr(qa[2].validator, "factory", Unavailable())
    with pytest.raises(DomainError, match="qa_database_and_audit_unavailable"):
        run(qa)
    assert not qa[3].calls


def test_agent_uploaded_attestation_has_no_authority(qa):
    from hogwarts.artifacts.contracts import ArtifactUpload

    p, built, runner, driver = qa
    w = p[0][0]
    fake = runner.store.put(
        w["actor"],
        w["run"],
        p[0][1].id,
        p[0][2].id,
        w["token"],
        "forged_attestation",
        ArtifactUpload(kind="report", media_type="application/json"),
        b'{"issuer":"trusted-qa-runner.v1","outcome":"pass","verified":true}',
    )
    assert fake.assessment.status == "unverified"
    driver.outcome = "fail"
    assert run(qa).outcome == "fail" and driver.calls.count("execute") == 1


def test_intent_commit_failure_has_no_side_effect(qa, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    original = qa[2]._state
    fired = False

    def fail_intent(s, actor, row, j, state, **changes):
        nonlocal fired
        if state == "EXECUTING" and not fired:
            fired = True
            raise SQLAlchemyError("intent rollback")
        return original(s, actor, row, j, state, **changes)

    monkeypatch.setattr(qa[2], "_state", fail_intent)
    with pytest.raises(DomainError):
        run(qa)
    assert not qa[3].calls and state(qa)["state"] == "PENDING"
    assert run(qa).outcome == "pass"
    assert qa[3].calls.count("execute") == 1


def test_partial_capsule_cannot_recover_or_reexecute(qa, monkeypatch):
    def partial(row, *args):
        path = qa[2].vault.objects.path(row.id)
        path.write_bytes(b"partial-result")
        raise OSError("interrupted capsule write")

    monkeypatch.setattr(qa[2].vault, "seal", partial)
    with pytest.raises(DomainError):
        run(qa)
    with pytest.raises(DomainError):
        run(qa)
    assert qa[3].calls.count("execute") == 1


@pytest.mark.parametrize("filename", ["credentials.json", "id_rsa", "private.pem"])
def test_sensitive_source_not_copied_to_qa(outputs, filename):
    from hogwarts.patches.builder import LocalGitPatchBuilder
    from test_outputs import archive
    from test_outputs import run as import_outputs

    o = outputs
    w = o[0]
    o[3].driver.data = archive([(filename, b"source content")])
    imported = import_outputs(o)
    builder = LocalGitPatchBuilder(o[5], w["root"].parent / "sensitive-patch")
    built = builder.build(w["actor"], w["run"], imported.id, w["token"])
    driver = QADriver()
    runner = LocalTrustedQARunner(
        builder, w["root"].parent / "qa", driver, QAProfileRegistry([QAProfile("unit", IMAGE)])
    )
    with pytest.raises(DomainError):
        runner.run(w["actor"], w["run"], built.build_id, "unit", w["token"])
    assert not driver.calls


def test_cleanup_pending_is_tombstone(qa, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    original = qa[2]._state

    def rollback_intent(s, actor, row, j, stage, **changes):
        if stage == "EXECUTING":
            raise SQLAlchemyError("intent rolled back")
        return original(s, actor, row, j, stage, **changes)

    monkeypatch.setattr(qa[2], "_state", rollback_intent)
    with pytest.raises(DomainError):
        run(qa)
    monkeypatch.setattr(qa[2], "_state", original)
    w = qa[0][0][0]
    qa[2].cleanup(w["actor"], w["run"], state(qa)["qa_id"], w["token"])
    with pytest.raises(DomainError, match="qa_cleanup_tombstone"):
        run(qa)
    assert "execute" not in qa[3].calls


def test_cleanup_race_before_create_cannot_execute(qa, monkeypatch):
    runner = qa[2]
    original = runner._execute
    w = qa[0][0][0]

    def cleanup_first(j, *args):
        runner.cleanup(w["actor"], w["run"], j.qa_id, w["token"])
        return original(j, *args)

    monkeypatch.setattr(runner, "_execute", cleanup_first)
    assert run(qa).outcome == "unknown_execution"
    assert "execute" not in qa[3].calls and "create" not in qa[3].calls
