import pytest
from conftest import post
from hogwarts.auth import Principal
from hogwarts.db import sessions
from hogwarts.models import GitWorkspace
from hogwarts.workspaces.contracts import RepositoryGrant, WorkspaceError
from hogwarts.workspaces.manager import GitWorkspaceManager
from sqlalchemy import select
from test_workspaces import git


@pytest.fixture
def queued_workspace(api, seed, engine, tmp_path):
    """Same real M1/Git boundary, with claim deferred to the signed Worker."""
    repo = tmp_path / "queued repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "RC1 Tests")
    git(repo, "config", "user.email", "rc1@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "src").mkdir()
    (repo / "src/main.txt").write_bytes(b"before\n")
    (repo / "src/delete.txt").write_bytes(b"delete\n")
    (repo / "src/data.bin").write_bytes(b"\0old\xff")
    (repo / "README.md").write_bytes(b"protected\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "trusted fixture")
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    seed["prototype"].update(base_commit=sha, allowed_paths=["src"])
    p = post(api, "prototypes", seed).json()
    q = post(api, f"prototypes/{p['id']}/start", {"expected_version": 1})
    assert q.status_code == 200
    grant = RepositoryGrant(
        seed["project_id"], repo, "https://github.com/example/authorized.git", ("src", "tests")
    )
    root = tmp_path / "workspaces"
    factory = sessions(engine)
    manager = GitWorkspaceManager(factory, root, (grant,))
    yield {
        "manager": manager,
        "actor": Principal("worker:engineer", "local-owner", "worker", "engineer"),
        "run": q.json()["run_id"],
        "token": None,
        "repo": repo,
        "sha": sha,
        "root": root,
        "grant": grant,
        "proposal": p["id"],
        "factory": factory,
    }
    with factory() as s:
        rows = list(s.scalars(select(GitWorkspace)))
    for row in rows:
        if (root / row.id / ".git").is_file():
            try:
                manager.cleanup(Principal("human:local-owner", "local-owner", "human"), row.run_id)
            except WorkspaceError:
                pass
