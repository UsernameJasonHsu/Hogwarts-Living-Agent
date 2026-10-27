"""Real Linux Docker fencing, not a fake driver/kernel claim."""

import json
import os
import sys
from pathlib import Path

import pytest
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.leases.contracts import Heartbeat
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import IdempotencyRecord, MergeRequest, WorkflowEvent
from hogwarts.outputs.contracts import OutputGrant
from hogwarts.outputs.driver import OutputController, OutputDockerDriver
from hogwarts.outputs.importer import LocalOutputImporter
from hogwarts.patches.builder import LocalGitPatchBuilder
from hogwarts.qa.driver import QADockerDriver
from hogwarts.qa.profiles import QAProfileRegistry
from hogwarts.qa.runner import LocalTrustedQARunner
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.service import DomainError
from hogwarts.verification.orchestrator import LocalPrototypeVerificationOrchestrator
from hogwarts.verification.profiles import ReviewQAProfile
from sqlalchemy import func, select
from test_verification import HUMAN, review, verify
from test_workspaces import workspace as workspace


@pytest.mark.parametrize(
    "scenario", ["qa_revoke", "review_revoke", "verification_revoke", "heartbeat", "stop"]
)
def test_real_linux_fencing_and_safe_stop(workspace, scenario, monkeypatch):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_REVIEW_QA_IMAGE"):
        pytest.skip("Real lease fencing requires Linux Docker and approved images")
    w = workspace
    root = w["root"].parent
    driver = OutputDockerDriver("m2-4-1-real", os.environ["HOGWARTS_OUTPUT_READER_IMAGE"])
    c = OutputController(
        w["manager"],
        driver,
        (RuntimeProfile("capture", os.environ["HOGWARTS_SANDBOX_IMAGE"], lifetime_seconds=60),),
    )
    store = LocalArtifactStore(w["manager"], root / "store")
    importer = LocalOutputImporter(c, store, root / "import", (OutputGrant("capture", "src"),))
    builder = LocalGitPatchBuilder(importer, root / "patch")
    image = os.environ["HOGWARTS_REVIEW_QA_IMAGE"]
    qa = LocalTrustedQARunner(
        builder,
        root / "qa",
        QADockerDriver("m2-4-1-real"),
        QAProfileRegistry([ReviewQAProfile("acceptance", image)]),
    )
    o = LocalPrototypeVerificationOrchestrator(qa, root / "verification")
    v = (w, o, driver, None)
    leases = DurableLeaseManager(w["factory"])
    observed = {}
    try:
        if scenario == "heartbeat":
            result = leases.heartbeat(
                w["actor"],
                w["run"],
                Heartbeat(lease_token=w["token"], generation=1, expected_version=1),
                "real-heartbeat",
            )
            assert result.version == 2
            info = verify(v)
            assert info.status == "READY_FOR_HUMAN_REVIEW"
            assert review(v, info)["merged"] is False
        elif scenario == "stop":
            ws = w["manager"].create(w["actor"], w["run"], w["token"])
            c.create(w["actor"], w["run"], ws.id, w["token"], "capture")
            c.start(w["actor"], w["run"], w["token"])
            leases.revoke(HUMAN, w["run"], 1, "stop-revoke")
            result = leases.stop(HUMAN, w["run"], lambda: c.stop(HUMAN, w["run"]))
            assert result.stop_state == "CONFIRMED"
            with pytest.raises(DomainError):
                c.execute(w["actor"], w["run"], w["token"], "late")
        elif scenario == "qa_revoke":
            execute = qa.driver.execute

            def revoked(*args, **kwargs):
                actual = execute(*args, **kwargs)
                observed.update(actual.model_dump(mode="json"))
                assert actual.outcome == "pass" and actual.exit_code == 0
                leases.revoke(HUMAN, w["run"], 1, "qa-revoke")
                return actual

            monkeypatch.setattr(qa.driver, "execute", revoked)
            with pytest.raises(DomainError):
                verify(v)
            with pytest.raises(DomainError):
                verify(v)
            with o.factory() as s:
                assert (
                    s.scalar(
                        select(func.count())
                        .select_from(WorkflowEvent)
                        .where(WorkflowEvent.event_type == "qa.executing")
                    )
                    == 1
                )
                journals = list(
                    s.scalars(
                        select(IdempotencyRecord).where(
                            IdempotencyRecord.key.like("qa-runner.v1:%")
                        )
                    )
                )
                assert len(journals) == 1
                assert journals[0].response["state"] == "EXECUTING"
                assert journals[0].response["attestation_artifact_id"] is None
        elif scenario == "verification_revoke":
            original = store.put

            def revoked_bundle(*args, **kwargs):
                if args[5].startswith("vb_"):
                    leases.revoke(HUMAN, w["run"], 1, "bundle-revoke")
                return original(*args, **kwargs)

            monkeypatch.setattr(store, "put", revoked_bundle)
            with pytest.raises(DomainError):
                verify(v)
            with o.factory() as s:
                assert s.scalar(select(func.count()).select_from(MergeRequest)) == 0
                assert not s.scalar(
                    select(WorkflowEvent).where(WorkflowEvent.event_type == "verification.complete")
                )

        else:
            info = verify(v)
            assert info.status == "READY_FOR_HUMAN_REVIEW"
            leases.revoke(HUMAN, w["run"], 1, "review-revoke")
            with pytest.raises(DomainError):
                review(v, info)
            with o.factory() as s:
                assert s.scalar(select(func.count()).select_from(MergeRequest)) == 0
        folder = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
        if folder:
            dest = Path(folder) / ("fence-" + scenario + "-" + w["run"])
            dest.mkdir(parents=True)
            with o.factory() as s:
                events = [
                    {"event": e.event_type, "source": e.source, "payload": e.payload}
                    for e in s.scalars(
                        select(WorkflowEvent).where(WorkflowEvent.aggregate_id == w["run"])
                    )
                ]
            (dest / "audit.json").write_text(json.dumps(events, indent=2))
            if observed:
                (dest / "real-qa-observation.json").write_text(json.dumps(observed, indent=2))
    finally:
        o.cleanup(HUMAN, w["run"])
