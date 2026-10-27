"""Trusted SIGKILL fixture helper, never executed inside workload containers."""

import json
import os
import signal
import sys
from pathlib import Path

from formal_support import real_formal
from hogwarts.auth import Principal
from hogwarts.db import make_engine, sessions
from hogwarts.workspaces.contracts import RepositoryGrant
from hogwarts.workspaces.manager import GitWorkspaceManager
from process_environment import source_identity, source_root


def main():
    source = source_identity()
    path = Path(sys.argv[1]).resolve()
    if not path.is_relative_to((source_root() / "test-results/tmp").resolve()):
        raise ValueError("unsafe_fixture_configuration")
    cfg = json.loads(path.read_text(encoding="utf8"))
    engine = make_engine(cfg["database"])
    manager = GitWorkspaceManager(
        sessions(engine),
        Path(cfg["workspace_root"]),
        (
            RepositoryGrant(
                cfg["project"], Path(cfg["repository"]), cfg["repo_ref"], ("src", "tests")
            ),
        ),
    )
    g, _ = real_formal(
        manager, Path(cfg["private_root"]), cfg["controller"], executable=cfg["docker"]
    )
    actor = Principal("worker:engineer", "local-owner", "worker", "engineer")

    def crash(actor, run_id, key, token, profile):
        r = g.quota.reserve(actor, run_id, key, token, profile)
        g.quota._transition(actor, run_id, r.reservation_id, token, "RESERVED", "IN_USE")
        Path(cfg["marker"]).write_text(
            json.dumps(
                {
                    "phase": "quota_in_use_committed",
                    "pid": os.getpid(),
                    "run_id": run_id,
                    "source": source,
                }
            ),
            encoding="utf8",
        )
        os.kill(os.getpid(), signal.SIGKILL)

    g.operational.run = crash
    g.execute(actor, cfg["run"], cfg["version"], cfg["token"], "process-crash")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "code", "fixture_failed"),
                }
            )
        )
        sys.exit(2)
