"""Real SIGKILL crash probe, trusted controller code only, never a sandbox workload."""

import json
import os
import signal
import sys
from pathlib import Path

from hogwarts.auth import Principal
from hogwarts.db import make_engine, sessions
from hogwarts.models import WorkflowEvent
from hogwarts.workspaces.contracts import RepositoryGrant
from hogwarts.workspaces.manager import GitWorkspaceManager
from recovery_support import real_orchestrator
from sqlalchemy import event
from sqlalchemy.orm import Session


def main():
    path = Path(sys.argv[1]).resolve()
    assert path.is_relative_to(Path("test-results/tmp").resolve())
    config = json.loads(path.read_text(encoding="utf8"))
    engine = make_engine(config["database"])
    grant = RepositoryGrant(
        config["project"],
        Path(config["repository"]),
        "https://github.com/example/authorized.git",
        ("src", "tests"),
    )
    manager = GitWorkspaceManager(sessions(engine), Path(config["root"]) / "workspaces", (grant,))
    o = real_orchestrator(
        manager, config["root"], config["controller"], config["docker_executable"]
    )
    actor = Principal("worker:engineer", "local-owner", "worker", "engineer")
    mode = sys.argv[2]

    def die(phase, **data):
        Path(config["marker"]).write_text(
            json.dumps({"phase": phase, "pid": os.getpid(), "run_id": config["run"], **data}),
            encoding="utf8",
        )
        os.kill(os.getpid(), signal.SIGKILL)

    put = o.store.put

    def interrupted(*args, **kwargs):
        key = args[5]
        if mode == "verification_sealed" and key.startswith("vb_"):
            die("verification_capsule_sealed_before_publication")
        result = put(*args, **kwargs)
        if mode == "artifact_partial" and key.startswith("qa_"):
            die(
                "qa_first_artifact_committed",
                artifact_id=result.artifact.artifact_id,
                sha256=result.artifact.sha256,
            )
        return result

    o.store.put = interrupted
    execute = o.qa.driver.execute

    def unsealed(*args, **kwargs):
        result = execute(*args, **kwargs)
        die(
            "qa_execution_completed_without_capsule",
            exit_code=result.exit_code,
            container_id=result.container_id,
            image_id=result.image_id,
        )

    if mode == "qa_unsealed":
        o.qa.driver.execute = unsealed
    if mode == "db_commit":

        def before_commit(s):
            if any(
                isinstance(x, WorkflowEvent) and x.event_type == "verification.complete"
                for x in s.new
            ):
                die("verification_final_transaction_before_commit")

        event.listen(Session, "before_commit", before_commit)
    o.verify(actor, config["run"], config["token"], "capture", "acceptance")
    return 3  # The selected crash boundary must actually have been reached.


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            json.dumps({"error_type": type(error).__name__, "code": getattr(error, "code", None)})
        )
        sys.exit(2)
