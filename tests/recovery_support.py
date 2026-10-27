"""Trusted test construction only; never supplied to workload containers."""

import os
from pathlib import Path

from hogwarts.artifacts.store import LocalArtifactStore
from hogwarts.outputs.contracts import OutputGrant
from hogwarts.outputs.driver import OutputController, OutputDockerDriver
from hogwarts.outputs.importer import LocalOutputImporter
from hogwarts.patches.builder import LocalGitPatchBuilder
from hogwarts.qa.driver import QADockerDriver
from hogwarts.qa.profiles import QAProfileRegistry
from hogwarts.qa.runner import LocalTrustedQARunner
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.verification.orchestrator import LocalPrototypeVerificationOrchestrator
from hogwarts.verification.profiles import ReviewQAProfile


def real_orchestrator(manager, root, controller_id, executable=None):
    root = Path(root)
    c = OutputController(
        manager,
        OutputDockerDriver(
            controller_id, os.environ["HOGWARTS_OUTPUT_READER_IMAGE"], executable=executable
        ),
        (RuntimeProfile("capture", os.environ["HOGWARTS_SANDBOX_IMAGE"], lifetime_seconds=60),),
    )
    store = LocalArtifactStore(manager, root / "store")
    importer = LocalOutputImporter(c, store, root / "import", (OutputGrant("capture", "src"),))
    builder = LocalGitPatchBuilder(importer, root / "patch")
    qa = LocalTrustedQARunner(
        builder,
        root / "qa",
        QADockerDriver(controller_id, executable=executable),
        QAProfileRegistry([ReviewQAProfile("acceptance", os.environ["HOGWARTS_REVIEW_QA_IMAGE"])]),
    )
    return LocalPrototypeVerificationOrchestrator(qa, root / "verification")
