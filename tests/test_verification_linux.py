"""Real producer -> stopped reader -> import -> Git patch -> fresh QA -> human review."""

import os
import sys

import pytest
from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.models import ArtifactMetadata, Project, WorkflowEvent
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
from test_verification import HUMAN, bundle, review, verify
from test_workspaces import git
from test_workspaces import workspace as workspace


@pytest.mark.parametrize("scenario", ["pass", "fail", "zero", "tamper", "revoked", "restart"])
def test_real_end_to_end_verification_and_human_review(workspace, scenario, monkeypatch):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_REVIEW_QA_IMAGE"):
        pytest.skip("Real E2E requires Linux Docker and pinned review QA/runtime/reader images")
    w = workspace
    root = w["root"].parent
    driver = OutputDockerDriver("m2-3-5-real", os.environ["HOGWARTS_OUTPUT_READER_IMAGE"])
    c = OutputController(
        w["manager"],
        driver,
        (RuntimeProfile("capture", os.environ["HOGWARTS_SANDBOX_IMAGE"], lifetime_seconds=60),),
    )
    store = LocalArtifactStore(w["manager"], root / "store")
    importer = LocalOutputImporter(c, store, root / "import", (OutputGrant("capture", "src"),))
    builder = LocalGitPatchBuilder(importer, root / "patch")
    image = os.environ["HOGWARTS_REVIEW_QA_IMAGE"]
    if scenario in {"fail", "zero"}:
        image = os.environ["HOGWARTS_REVIEW_QA_" + scenario.upper() + "_IMAGE"]
    qa = LocalTrustedQARunner(
        builder,
        root / "qa",
        QADockerDriver("m2-3-5-real"),
        QAProfileRegistry([ReviewQAProfile("acceptance", image)]),
    )
    o = LocalPrototypeVerificationOrchestrator(qa, root / "verification")
    v = (w, o, driver, None)
    try:
        if scenario == "restart":
            original = store.put

            def injected(*args, **kwargs):
                if args[5].startswith("vb_"):
                    raise OSError("publication failure injection")
                return original(*args, **kwargs)

            monkeypatch.setattr(store, "put", injected)
            with pytest.raises(DomainError):
                verify(v)
            monkeypatch.setattr(store, "put", original)
            o = LocalPrototypeVerificationOrchestrator(qa, root / "verification")
            v = (w, o, driver, None)
        info = verify(v)
        b = bundle(v, info)
        expected = {"fail": "FAILED", "zero": "BLOCKED"}.get(scenario, "READY_FOR_HUMAN_REVIEW")
        assert info.status == expected
        if expected == "READY_FOR_HUMAN_REVIEW":
            assert b.tests_executed == 2
        evidence = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
        if evidence:
            from pathlib import Path

            folder = Path(evidence) / (scenario + "-" + w["run"])
            folder.mkdir(parents=True)
            (folder / "review-bundle.json").write_text(b.model_dump_json(indent=2))
            (folder / "verification-info.json").write_text(info.model_dump_json(indent=2))
            for ref in b.artifacts:
                obj = store.read(HUMAN, w["run"], ref.artifact_id)
                if ref.artifact_id == b.attestation_artifact_id:
                    (folder / "qa-attestation.json").write_bytes(obj.content)
                if obj.evidence.artifact.kind == "log":
                    (folder / (ref.artifact_id + ".log")).write_bytes(obj.content)
        if scenario == "tamper":
            with o.factory() as s:
                patch = s.scalar(
                    select(ArtifactMetadata).where(
                        ArtifactMetadata.evidence["kind"].as_string() == "patch"
                    )
                )
            file = store.objects.path(patch.id)
            file.chmod(0o600)
            file.write_bytes(b"tampered patch")
            file.chmod(0o400)
        if scenario == "revoked":
            with o.factory.begin() as s:
                s.get(Project, w["grant"].project_id).allowed_paths = ["revoked"]
        if scenario in {"fail", "zero", "tamper", "revoked"}:
            with pytest.raises(DomainError):
                review(v, info)
        else:
            assert review(v, info)["merged"] is False
        with o.factory() as s:
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(WorkflowEvent)
                    .where(
                        WorkflowEvent.aggregate_id == w["run"],
                        WorkflowEvent.event_type == "qa.executing",
                    )
                )
                == 1
            )
        assert git(w["repo"], "status", "--porcelain") == b""
    finally:
        o.cleanup(HUMAN, w["run"])
