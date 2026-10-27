import pytest
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.models import IdempotencyRecord, WorkflowEvent
from hogwarts.outputs.contracts import OutputGrant
from hogwarts.outputs.driver import OutputController
from hogwarts.outputs.importer import LocalOutputImporter
from hogwarts.patches.builder import LocalGitPatchBuilder
from hogwarts.qa.profiles import QAProfileRegistry
from hogwarts.qa.runner import LocalTrustedQARunner
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.service import DomainError
from hogwarts.verification.orchestrator import LocalPrototypeVerificationOrchestrator
from hogwarts.verification.profiles import ReviewQAProfile
from hogwarts.workspaces.manager import GitWorkspaceManager
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from test_formal import gateway, version
from test_outputs import CaptureDriver
from test_qa import QADriver
from test_verification import IMAGE
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def rebuild(v):
    w = v[0]
    manager = GitWorkspaceManager(w["manager"].factory, w["root"], (w["grant"],))
    root = w["root"].parent
    d = CaptureDriver()
    d.data = v[2].data
    c = OutputController(manager, d, (RuntimeProfile("capture", IMAGE, lifetime_seconds=60),))
    store = LocalArtifactStore(manager, root / "artifacts")
    importer = LocalOutputImporter(c, store, root / "imports", (OutputGrant("capture", "src"),))
    b = LocalGitPatchBuilder(importer, root / "patches")
    qd = QADriver()
    qa = LocalTrustedQARunner(
        b, root / "qa", qd, QAProfileRegistry([ReviewQAProfile("acceptance", IMAGE)])
    )
    o = LocalPrototypeVerificationOrchestrator(qa, root / "verification")
    return {**w, "manager": manager}, o, d, qd


def test_controller_recreation_returns_receipt_without_execution(verification):
    v = verification
    w = v[0]
    g = gateway(v)
    expected = version(w)
    first = g.execute(w["actor"], w["run"], expected, w["token"], "restart")
    new = rebuild(v)
    restarted = gateway(new)
    second = restarted.execute(w["actor"], w["run"], expected, w["token"], "restart")
    assert second == first and not new[3].calls and not new[2].calls


def test_final_commit_failure_is_not_replayed_after_recreation(verification):
    v = verification
    w = v[0]
    g = gateway(v)
    expected = version(w)
    fired = []

    def fail(s):
        if not fired and any(
            isinstance(x, WorkflowEvent) and x.event_type == "operations.formal_complete"
            for x in s.new
        ):
            fired.append(True)
            raise OperationalError("injected", {}, Exception("formal final commit failed"))

    event.listen(Session, "before_commit", fail)
    try:
        with pytest.raises(DomainError):
            g.execute(w["actor"], w["run"], expected, w["token"], "commit")
    finally:
        event.remove(Session, "before_commit", fail)
    assert fired and v[3].calls.count("execute") == 1
    new = rebuild(v)
    restarted = gateway(new)
    with pytest.raises(DomainError, match="formal_execution_uncertain"):
        restarted.execute(w["actor"], w["run"], expected, w["token"], "commit")
    with w["factory"]() as s:
        row = s.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key.like("formal-dispatch.v1:%"))
        )
        assert row.response["state"] == "DISPATCHING"
    assert not new[3].calls
