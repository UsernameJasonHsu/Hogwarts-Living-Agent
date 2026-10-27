"""Real M1/Git/Store/Import/Patch; explicitly fake sandbox and QA execution."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.auth import Principal
from hogwarts.models import (
    ApprovalRequest,
    ArtifactMetadata,
    IdempotencyRecord,
    MergeRequest,
    Project,
    Proposal,
    PrototypeRun,
    WorkflowEvent,
    now,
)
from hogwarts.outputs.contracts import OutputGrant
from hogwarts.outputs.driver import OutputController
from hogwarts.outputs.importer import LocalOutputImporter
from hogwarts.patches.builder import LocalGitPatchBuilder
from hogwarts.qa.profiles import QAProfileRegistry
from hogwarts.qa.runner import LocalTrustedQARunner
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.schemas import VersionCommand
from hogwarts.service import DomainError, cancel, command
from hogwarts.verification.contracts import ReviewBundle
from hogwarts.verification.orchestrator import LocalPrototypeVerificationOrchestrator
from hogwarts.verification.profiles import ReviewQAProfile
from sqlalchemy import func, select
from test_outputs import CaptureDriver, archive
from test_qa import QADriver
from test_workspaces import git
from test_workspaces import workspace as workspace

IMAGE = "sha256:" + "b" * 64
HUMAN = Principal("human:local-owner", "local-owner", "human")


@pytest.fixture
def verification(workspace):
    w = workspace
    root = w["root"].parent
    driver = CaptureDriver()
    driver.data = archive([("prototype.txt", b"deterministic prototype\n")])
    c = OutputController(w["manager"], driver, (RuntimeProfile("capture", IMAGE),))
    store = LocalArtifactStore(w["manager"], root / "artifacts")
    importer = LocalOutputImporter(c, store, root / "imports", (OutputGrant("capture", "src"),))
    builder = LocalGitPatchBuilder(importer, root / "patches")
    qa_driver = QADriver()
    runner = LocalTrustedQARunner(
        builder, root / "qa", qa_driver, QAProfileRegistry([ReviewQAProfile("acceptance", IMAGE)])
    )
    orchestrator = LocalPrototypeVerificationOrchestrator(runner, root / "verification")
    return w, orchestrator, driver, qa_driver


def verify(v, **changes):
    w, o, _, _ = v
    args = dict(
        actor=w["actor"],
        run_id=w["run"],
        lease_token=w["token"],
        runtime_profile="capture",
        qa_profile="acceptance",
    )
    args.update(changes)
    return o.verify(**args)


def review(v, info, **changes):
    args = dict(
        actor=HUMAN,
        run_id=v[0]["run"],
        verification_id=info.verification_id,
        bundle_sha256=info.bundle_sha256,
        expected_version=info.proposal_version,
        decision="ACCEPTED",
        idempotency_key="review-command",
    )
    args.update(changes)
    return v[1].review(**args)


def bundle(v, info):
    obj = v[1].store.read(HUMAN, v[0]["run"], info.bundle_artifact_id)
    assert obj.evidence.assessment.status == "unverified"
    return ReviewBundle.model_validate_json(obj.content)


def test_success_bundle_review_and_idempotent_merge_intent(verification):
    v = verification
    info = verify(v)
    assert info.status == "READY_FOR_HUMAN_REVIEW" and verify(v) == info
    b = bundle(v, info)
    assert b.tests_executed == 2 and len(b.artifacts) == 8
    accepted = review(v, info)
    assert accepted == review(v, info) and accepted["merged"] is False
    assert accepted["merge_request_id"]
    with v[1].factory() as s:
        assert s.scalar(select(func.count()).select_from(MergeRequest)) == 1
        assert s.get(PrototypeRun, v[0]["run"]).status == "READY_FOR_HUMAN_REVIEW"
        assert s.get(Proposal, v[0]["proposal"]).status == "ACCEPTED"
        assert s.scalar(
            select(WorkflowEvent).where(WorkflowEvent.event_type == "verification.human_decided")
        )
    assert v[3].calls.count("execute") == v[2].captures == 1
    assert git(v[0]["repo"], "status", "--porcelain") == b""


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("fail", "FAILED"),
        ("timeout", "FAILED"),
        ("infrastructure_error", "FAILED"),
        ("unknown_execution", "UNKNOWN"),
    ],
)
def test_nonpassing_evidence_never_becomes_ready(verification, outcome, status):
    v = verification
    v[3].outcome = outcome
    info = verify(v)
    assert info.status == status and bundle(v, info).tests_executed == 0
    with pytest.raises(DomainError):
        review(v, info)


@pytest.mark.parametrize(
    "bad", ["run", "principal", "owner", "lease", "expired", "profile", "runtime"]
)
def test_denials_before_execution(verification, bad):
    v = verification
    args = {}
    if bad == "run":
        args["run_id"] = str(uuid4())
    if bad == "principal":
        args["actor"] = replace(v[0]["actor"], id="other")
    if bad == "owner":
        args["actor"] = replace(v[0]["actor"], owner_id="other")
    if bad == "lease":
        args["lease_token"] = "wrong"
    if bad == "profile":
        args["qa_profile"] = "unapproved"
    if bad == "runtime":
        args["runtime_profile"] = "shell"
    if bad == "expired":
        with v[1].factory.begin() as s:
            s.get(PrototypeRun, v[0]["run"]).lease_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        verify(v, **args)
    assert v[3].calls == []


@pytest.mark.parametrize(
    "bad", ["hash", "version", "worker", "owner", "expired", "cancel", "grant", "profile"]
)
def test_human_review_revalidates_binding_and_policy(verification, bad):
    v = verification
    info = verify(v)
    changes = {}
    if bad == "hash":
        changes["bundle_sha256"] = "a" * 64
    if bad == "version":
        changes["expected_version"] = info.proposal_version + 1
    if bad == "worker":
        changes["actor"] = v[0]["actor"]
    if bad == "owner":
        changes["actor"] = replace(HUMAN, owner_id="other")
    if bad == "expired":
        with v[1].factory.begin() as s:
            s.get(ApprovalRequest, info.approval_id).expires_at = now() - timedelta(seconds=1)
    if bad == "cancel":
        command(
            v[1].factory,
            HUMAN,
            "cancel",
            "cancel",
            v[0]["proposal"],
            {},
            lambda s: cancel(
                s,
                HUMAN,
                v[0]["proposal"],
                VersionCommand(expected_version=info.proposal_version),
                "cancel",
            ),
        )
    if bad == "grant":
        with v[1].factory.begin() as s:
            s.get(Project, v[0]["grant"].project_id).allowed_paths = ["other"]
    if bad == "profile":
        v[1].qa.registry = QAProfileRegistry([ReviewQAProfile("acceptance", "sha256:" + "e" * 64)])
    with pytest.raises(DomainError):
        review(v, info, **changes)
    with v[1].factory() as s:
        assert s.scalar(select(func.count()).select_from(MergeRequest)) == 0


@pytest.mark.parametrize("decision", ["REJECTED", "DEFERRED"])
def test_human_nonacceptance_has_no_intent(verification, decision):
    v = verification
    result = review(v, verify(v), decision=decision)
    assert result["merge_request_id"] is None and result["merged"] is False


@pytest.mark.parametrize("target", ["bundle", "patch", "proof", "attestation", "log", "missing"])
def test_postpublication_tampering_blocks_human_acceptance(verification, target):
    v = verification
    info = verify(v)
    b = bundle(v, info)
    with v[1].factory() as s:
        rows = {r.id: r for r in s.scalars(select(ArtifactMetadata))}
    identifier = info.bundle_artifact_id
    if target in {"patch", "proof"}:
        identifier = b.patch_artifact_id if target == "patch" else b.build_proof_artifact_id
    if target == "attestation":
        identifier = b.attestation_artifact_id
    if target == "log":
        identifier = next(r.id for r in rows.values() if r.evidence["kind"] == "log")
    path = v[1].store.objects.path(identifier)
    if target == "missing":
        path.chmod(0o600)
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"tampered")
        path.chmod(0o400)
    with pytest.raises(DomainError):
        review(v, info)


def test_concurrent_request_cannot_revoke_or_repeat_execution(verification):
    v = verification
    v[3].release = Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(verify, v)
        assert v[3].running.wait(120)
        try:
            with pytest.raises(DomainError):
                verify(v)
        finally:
            v[3].release.set()
        assert first.result().status == "READY_FOR_HUMAN_REVIEW"
    assert v[3].calls.count("execute") == 1


def test_publication_failure_restart_never_reexecutes(verification, monkeypatch):
    v = verification
    original = v[1].store.put

    def fail(*args, **kwargs):
        if args[5].startswith("vb_"):
            raise OSError("injected")
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1].store, "put", fail)
    with pytest.raises(DomainError):
        verify(v)
    monkeypatch.setattr(v[1].store, "put", original)
    restarted = LocalPrototypeVerificationOrchestrator(v[1].qa, v[1].vault.objects.root)
    info = verify((v[0], restarted, v[2], v[3]))
    assert info.status == "READY_FOR_HUMAN_REVIEW" and v[3].calls.count("execute") == 1


def test_uncertain_execution_never_replayed(verification, monkeypatch):
    v = verification
    monkeypatch.setattr(
        v[1].controller, "start", lambda *a: (_ for _ in ()).throw(OSError("crash"))
    )
    with pytest.raises(DomainError):
        verify(v)
    with pytest.raises(DomainError):
        verify(v)
    with v[1].factory() as s:
        row = s.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.key.like("prototype-verification.v1:%")
            )
        )
        assert row.response["state"] == "UNKNOWN"
    assert v[3].calls == []


def test_revocation_during_qa_blocks_presentation(verification, monkeypatch):
    v = verification
    original = v[3].execute

    def revoked(*args):
        result = original(*args)
        with v[1].factory.begin() as s:
            s.get(Project, v[0]["grant"].project_id).allowed_paths = ["other"]
        return result

    monkeypatch.setattr(v[3], "execute", revoked)
    with pytest.raises(DomainError):
        verify(v)
    with v[1].factory() as s:
        assert s.get(PrototypeRun, v[0]["run"]).status != "READY_FOR_HUMAN_REVIEW"


def test_cleanup_idempotent_and_historical_review(verification):
    v = verification
    info = verify(v)
    v[1].cleanup(HUMAN, v[0]["run"])
    v[1].cleanup(HUMAN, v[0]["run"])
    assert review(v, info)["merged"] is False


@pytest.mark.parametrize(
    "bad", ["qa-profile", "qa-patch", "qa-image", "build-proof", "missing-attestation"]
)
def test_gate_rejects_mismatched_or_missing_evidence(verification, monkeypatch, bad):
    v = verification
    original = v[1].qa.run

    def injected(*args):
        result = original(*args)
        with v[1].factory.begin() as s:
            row = s.get(IdempotencyRecord, result.qa_id)
            if bad == "qa-profile":
                row.response = {**row.response, "profile_sha256": "a" * 64}
            if bad == "qa-image":
                path = v[1].store.objects.path(result.attestation_artifact_id)
                body = json.loads(path.read_bytes())
                body["image_id"] = "sha256:" + "e" * 64
                path.chmod(0o600)
                path.write_text(json.dumps(body))
                path.chmod(0o400)
            if bad == "qa-patch":
                row.response = {**row.response, "build_id": str(uuid4())}
            if bad in {"build-proof", "missing-attestation"}:
                build_row = s.get(IdempotencyRecord, row.response["build_id"])
                identifier = (
                    build_row.response["proof_artifact_id"]
                    if bad == "build-proof"
                    else result.attestation_artifact_id
                )
                path = v[1].store.objects.path(identifier)
                path.chmod(0o600)
                path.write_bytes(b"tampered evidence")
                path.chmod(0o400)
        return result

    monkeypatch.setattr(v[1].qa, "run", injected)
    with pytest.raises(DomainError):
        verify(v)
    with v[1].factory() as s:
        assert s.get(PrototypeRun, v[0]["run"]).status != "READY_FOR_HUMAN_REVIEW"


def test_db_finalize_failure_restores_only_publication(verification, monkeypatch):
    from sqlalchemy.exc import OperationalError

    v = verification
    original = v[1]._state

    def injected(*args, **kwargs):
        if kwargs.get("state") == "COMPLETE":
            raise OperationalError("injected", None, None)
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1], "_state", injected)
    with pytest.raises(DomainError):
        verify(v)
    monkeypatch.setattr(v[1], "_state", original)
    assert verify(v).status == "READY_FOR_HUMAN_REVIEW"
    assert v[3].calls.count("execute") == 1


def test_legacy_human_endpoint_cannot_bypass_bundle_gate(verification):
    from hogwarts.schemas import HumanDecision
    from hogwarts.service import decision

    v = verification
    info = verify(v)
    with pytest.raises(DomainError):
        command(
            v[1].factory,
            HUMAN,
            "legacy",
            "decision",
            v[0]["proposal"],
            {},
            lambda s: decision(
                s,
                HUMAN,
                v[0]["proposal"],
                HumanDecision(expected_version=info.proposal_version, decision="ACCEPTED"),
                "legacy",
            ),
        )


def test_database_and_audit_outage_is_not_reported_success(verification, monkeypatch):
    from sqlalchemy.exc import OperationalError

    class Unavailable:
        def __call__(self):
            raise OperationalError("injected", None, None)

        def begin(self):
            raise OperationalError("injected", None, None)

    v = verification
    monkeypatch.setattr(v[1], "factory", Unavailable())
    with pytest.raises(DomainError, match="verification_database_and_audit_unavailable"):
        verify(v)
    assert v[3].calls == []


def test_concurrent_human_decisions_create_one_bundle_bound_intent(verification):
    v = verification
    info = verify(v)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: review(v, info), range(2)))
    assert results[0] == results[1]
    with v[1].factory() as s:
        intent = s.get(MergeRequest, results[0]["merge_request_id"])
        assert intent.approval_id == info.approval_id
        assert s.scalar(select(func.count()).select_from(MergeRequest)) == 1


def test_cancellation_during_execution_is_terminal(verification, monkeypatch):
    v = verification
    original = v[3].execute

    def cancelled(*args):
        result = original(*args)
        with v[1].factory() as s:
            version = s.get(Proposal, v[0]["proposal"]).version
        command(
            v[1].factory,
            HUMAN,
            "cancel-running",
            "cancel",
            v[0]["proposal"],
            {},
            lambda s: cancel(
                s,
                HUMAN,
                v[0]["proposal"],
                VersionCommand(expected_version=version),
                "cancel-running",
            ),
        )
        return result

    monkeypatch.setattr(v[3], "execute", cancelled)
    with pytest.raises(DomainError):
        verify(v)
    with v[1].factory() as s:
        assert s.get(PrototypeRun, v[0]["run"]).status == "CANCELLED"
