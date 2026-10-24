import hashlib
import os
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from conftest import headers, post
from hogwarts.auth import Principal
from hogwarts.db import sessions
from hogwarts.models import GitWorkspace, Proposal, PrototypeRun, WorkflowEvent, now
from hogwarts.workspaces.contracts import RepositoryGrant, WorkspaceError
from hogwarts.workspaces.git import relative_path, unambiguous
from hogwarts.workspaces.manager import GitWorkspaceManager
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session


def git(repo, *arguments, data=None):
    result = subprocess.run(
        [
            shutil.which("git"),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(repo),
            *arguments,
        ],
        input=data,
        capture_output=True,
        check=True,
        timeout=20,
    )
    return result.stdout


def overwrite_metadata(path, data):
    # Git for Windows marks .git hidden; OPEN_EXISTING is needed instead of
    # CREATE_ALWAYS ('wb') for this deliberate fixture-tampering test.
    path.chmod(path.stat().st_mode | stat.S_IWRITE)
    with path.open("r+b") as file:
        file.write(data)
        file.truncate()


@pytest.fixture
def workspace(api, seed, engine, tmp_path):
    repo = tmp_path / "authorized repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Workspace Tests")
    git(repo, "config", "user.email", "workspace@example.invalid")
    git(repo, "config", "core.autocrlf", "false")
    (repo / "src").mkdir()
    (repo / "src/main.txt").write_bytes(b"before\n")
    (repo / "src/delete.txt").write_bytes(b"delete\n")
    (repo / "src/data.bin").write_bytes(b"\0old\xff")
    (repo / "src/.gitignore").write_bytes(b"ignored.txt\n")
    (repo / "README.md").write_bytes(b"source protected\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fixture")
    sha = git(repo, "rev-parse", "HEAD").decode().strip()
    seed["prototype"].update(base_commit=sha, allowed_paths=["src"])
    proposal = post(api, "prototypes", seed).json()
    queued = post(api, f"prototypes/{proposal['id']}/start", {"expected_version": 1}).json()
    lease = post(
        api, f"runs/{queued['run_id']}/claim", {"expected_version": queued["version"]}, "worker"
    ).json()
    actor = Principal("worker:engineer", "local-owner", "worker", "engineer")
    grant = RepositoryGrant(
        seed["project_id"], repo, "https://github.com/example/authorized.git", ("src", "tests")
    )
    root = tmp_path / "workspaces"
    manager = GitWorkspaceManager(sessions(engine), root, (grant,))
    yield {
        "manager": manager,
        "actor": actor,
        "run": queued["run_id"],
        "token": lease["lease_token"],
        "repo": repo,
        "sha": sha,
        "root": root,
        "grant": grant,
        "proposal": proposal["id"],
        "factory": sessions(engine),
    }
    # Only our fixture repository/workspace: release registrations before fixture deletion.
    with Session(engine) as session:
        rows = list(session.scalars(select(GitWorkspace)))
    for row in rows:
        path = root / row.id
        if (path / ".git").is_file():
            # Tests that intentionally corrupt metadata restore it before returning.
            try:
                manager.cleanup(Principal("human:local-owner", "local-owner", "human"), row.run_id)
            except WorkspaceError:
                pass


def create(w):
    return w["manager"].create(w["actor"], w["run"], w["token"])


def export(w):
    return w["manager"].export_diff(w["actor"], w["run"], w["token"])


def cleanup(w):
    return w["manager"].cleanup(w["actor"], w["run"], w["token"])


def mutate_document(engine, w, **scope):
    with Session(engine) as session, session.begin():
        proposal = session.get(Proposal, w["proposal"])
        document = {**proposal.document, "prototype": {**proposal.document["prototype"], **scope}}
        proposal.document = document


def test_create_pinned_detached_and_source_unchanged(workspace):
    w = workspace
    index = (w["repo"] / ".git/index").read_bytes()
    info = create(w)
    assert info.state == "READY"
    assert git(info.path, "rev-parse", "HEAD").decode().strip() == w["sha"]
    assert (w["repo"] / ".git/worktrees" / info.id / "HEAD").read_text().strip() == w["sha"]
    assert git(w["repo"], "branch", "--show-current").strip() == b"main"
    assert git(w["repo"], "status", "--porcelain") == b""
    assert (w["repo"] / ".git/index").read_bytes() == index
    assert export(w).patch == b""


def test_export_all_changes_including_ignored_and_binary(workspace, engine):
    w = workspace
    info = create(w)
    source_index = (w["repo"] / ".git/index").read_bytes()
    own_index = (w["repo"] / ".git/worktrees" / info.id / "index").read_bytes()
    (info.path / "src/main.txt").write_bytes(b"after\n")
    (info.path / "src/delete.txt").unlink()
    (info.path / "src/new file.txt").write_bytes(b"new\n")
    (info.path / "src/ignored.txt").write_bytes(b"not hidden\n")
    (info.path / "src/data.bin").write_bytes(b"\0new\xfe")
    result = export(w)
    assert result.changed_files == (
        "src/data.bin",
        "src/delete.txt",
        "src/ignored.txt",
        "src/main.txt",
        "src/new file.txt",
    )
    assert b"GIT binary patch" in result.patch and b"+after" in result.patch
    assert result.sha256 == hashlib.sha256(result.patch).hexdigest()
    git(w["repo"], "apply", "--check", "--binary", data=result.patch)
    assert (w["repo"] / ".git/index").read_bytes() == source_index
    assert (w["repo"] / ".git/worktrees" / info.id / "index").read_bytes() == own_index
    assert (w["repo"] / "src/main.txt").read_bytes() == b"before\n"
    with Session(engine) as session:
        row = session.scalar(select(GitWorkspace))
        assert row.last_diff_sha256 == result.sha256
        events = list(
            session.scalars(
                select(WorkflowEvent).where(WorkflowEvent.event_type == "workspace.diff_exported")
            )
        )
        assert len(events) == 1 and events[0].source == "git_workspace"
        assert "after" not in str(events[0].payload) and w["token"] not in str(events[0].payload)


def test_staged_changes_export_without_mutating_index(workspace):
    w = workspace
    info = create(w)
    (info.path / "src/main.txt").write_bytes(b"staged\n")
    git(info.path, "add", "src/main.txt")
    assert b"+staged" in export(w).patch


@pytest.mark.parametrize("name", ["README.md", "outside.txt", "src-other/a.txt"])
def test_unauthorized_changes_rejected(workspace, name):
    w = workspace
    info = create(w)
    file = info.path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"forbidden")
    with pytest.raises(WorkspaceError, match="changed_path_not_authorized"):
        export(w)


def test_repeat_create_restart_and_cleanup_tombstone(workspace):
    w = workspace
    first = create(w)
    assert create(w) == first
    w["manager"] = GitWorkspaceManager(w["factory"], w["root"], (w["grant"],))
    assert create(w) == first
    result = cleanup(w)
    assert result.state == "CLEANED" and not first.path.exists()
    assert cleanup(w) == result
    with pytest.raises(WorkspaceError, match="workspace_not_creatable"):
        create(w)


@pytest.mark.parametrize("operation", ["create", "cleanup"])
def test_concurrent_idempotent_operations(workspace, engine, operation):
    w = workspace
    if operation == "cleanup":
        create(w)
    barrier = Barrier(2)

    def invoke(_):
        barrier.wait(timeout=10)
        # Separate managers; correctness is from DB/Git, not an instance mutex.
        manager = GitWorkspaceManager(w["factory"], w["root"], (w["grant"],))
        return getattr(manager, operation)(w["actor"], w["run"], w["token"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert results[0] == results[1]
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GitWorkspace)) == 1
        event_type = "workspace.ready" if operation == "create" else "workspace.cleaned"
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkflowEvent)
                .where(WorkflowEvent.event_type == event_type)
            )
            == 1
        )


@pytest.mark.parametrize("kind", ["missing_grant", "repo_ref", "scope", "owner", "agent", "lease"])
def test_default_deny(workspace, kind):
    w = workspace
    if kind == "missing_grant":
        w["manager"].grants.clear()
    elif kind == "repo_ref":
        g = w["grant"]
        w["manager"].grants[g.project_id] = RepositoryGrant(
            g.project_id, g.repository, "https://github.com/other/repo", ("src",)
        )
    elif kind == "scope":
        g = w["grant"]
        w["manager"].grants[g.project_id] = RepositoryGrant(
            g.project_id, g.repository, g.repo_ref, ("tests",)
        )
    elif kind == "owner":
        w["actor"] = Principal("worker:engineer", "other-owner", "worker", "engineer")
    elif kind == "agent":
        w["actor"] = Principal("worker:other", "local-owner", "worker", "other")
    else:
        w["token"] = "forged"
    with pytest.raises(Exception) as failure:
        create(w)
    assert getattr(failure.value, "status", None) in {403, 404}
    assert not list(w["root"].glob("????????-????-????-????-????????????"))


def test_expired_lease_blocks_export_but_allows_cleanup(workspace, engine):
    w = workspace
    create(w)
    with Session(engine) as session, session.begin():
        session.execute(update(PrototypeRun).values(lease_expires_at=now() - timedelta(seconds=1)))
    with pytest.raises(WorkspaceError, match="inactive_workspace_lease"):
        export(w)
    assert cleanup(w).state == "CLEANED"


def test_owner_cleanup_after_cancel(workspace, api):
    w = workspace
    create(w)
    proposal = api.get(f"/api/v1/prototypes/{w['proposal']}", headers=headers()).json()
    assert (
        post(
            api, f"prototypes/{w['proposal']}/cancel", {"expected_version": proposal["version"]}
        ).status_code
        == 200
    )
    human = Principal("human:local-owner", "local-owner", "human")
    assert w["manager"].cleanup(human, w["run"]).state == "CLEANED"


@pytest.mark.parametrize("sha", ["main", "a" * 7, "f" * 40])
def test_invalid_or_absent_sha(workspace, engine, sha):
    mutate_document(engine, workspace, base_commit=sha)
    with pytest.raises(WorkspaceError):
        create(workspace)


def test_blob_sha_is_not_commit(workspace, engine):
    blob = git(workspace["repo"], "hash-object", "-w", "--stdin", data=b"data").decode().strip()
    mutate_document(engine, workspace, base_commit=blob)
    with pytest.raises(WorkspaceError, match="not_a_commit"):
        create(workspace)


@pytest.mark.parametrize(
    "mode,name",
    [
        ("120000", "link"),
        ("160000", "module"),
        ("100644", "CON"),
        ("100644", ".GIT"),
        ("100644", ".gitattributes"),
    ],
)
def test_unsafe_git_tree_before_checkout(workspace, engine, mode, name):
    w = workspace
    sha = (
        w["sha"]
        if mode == "160000"
        else git(w["repo"], "hash-object", "-w", "--stdin", data=b"/outside/secret")
        .decode()
        .strip()
    )
    kind = "commit" if mode == "160000" else "blob"
    tree = (
        git(w["repo"], "mktree", "-z", data=f"{mode} {kind} {sha}\t{name}\0".encode())
        .decode()
        .strip()
    )
    commit = git(w["repo"], "commit-tree", tree, "-m", "unsafe fixture").decode().strip()
    mutate_document(engine, w, base_commit=commit)
    with pytest.raises(WorkspaceError):
        create(w)
    assert not list(w["root"].glob("????????-????-????-????-????????????"))


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "src/../escape",
        "src\\x",
        "src/.GIT/config",
        "src/x.",
        "src/CON.txt",
        "C:/x",
    ],
)
def test_unsafe_paths(path):
    with pytest.raises(WorkspaceError):
        relative_path(path)


@pytest.mark.parametrize("kind", ["include", "filter", "alternates"])
def test_untrusted_repository_configuration_rejected(workspace, kind):
    w = workspace
    if kind == "include":
        git(w["repo"], "config", "include.path", str(w["root"] / "never-read-sensitive"))
    elif kind == "filter":
        git(w["repo"], "config", "filter.evil.smudge", "arbitrary-command")
    else:
        (w["repo"] / ".git/objects/info/alternates").write_text("/outside/object-store")
    with pytest.raises(WorkspaceError, match="unsafe_repository_config"):
        create(w)


def test_checkout_hook_and_inherited_config_do_not_execute(workspace, monkeypatch):
    w = workspace
    sentinel = w["repo"].parent / "hook-ran"
    hook = w["repo"] / ".git/hooks/post-checkout"
    hook.write_text('#!/bin/sh\n touch "' + sentinel.as_posix() + '"\n', encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "include.path")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/outside/never-read")
    assert create(w).state == "READY"
    assert not sentinel.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_working_links_denied_without_touching_outside(workspace, engine, kind):
    w = workspace
    info = create(w)
    outside = w["repo"].parent / "outside sentinel"
    outside.write_bytes(b"private sentinel")
    link = info.path / "src/link"
    try:
        if kind == "symlink":
            link.symlink_to(outside)
        else:
            os.link(outside, link)
    except OSError as error:
        pytest.skip(f"Native link creation unavailable: {type(error).__name__}")
    try:
        with pytest.raises(WorkspaceError, match="unsafe_filesystem_entry"):
            export(w)
        with pytest.raises(WorkspaceError, match="unsafe_filesystem_entry"):
            cleanup(w)
        with Session(engine) as session:
            assert session.scalar(select(GitWorkspace)).state == "CLEANUP_FAILED"
        assert outside.read_bytes() == b"private sentinel"
    finally:
        link.unlink()
    assert cleanup(w).state == "CLEANED"


def test_git_pointer_tamper_denied(workspace):
    w = workspace
    info = create(w)
    file = info.path / ".git"
    original = file.read_bytes()
    overwrite_metadata(file, b"gitdir: /outside/never-read\n")
    try:
        with pytest.raises(WorkspaceError, match="workspace_provenance_mismatch"):
            export(w)
    finally:
        overwrite_metadata(file, original)


def test_changed_configuration_cannot_reuse_workspace(workspace, engine):
    w = workspace
    create(w)
    mutate_document(engine, w, allowed_paths=["src/main.txt"])
    with pytest.raises(WorkspaceError, match="workspace_request_conflict"):
        create(w)


def test_failed_create_has_durable_audit_and_cleanup(workspace, engine, monkeypatch):
    w = workspace

    def fail(*args):
        raise WorkspaceError(409, "git_command_failed")

    monkeypatch.setattr(w["manager"].git, "add", fail)
    with pytest.raises(WorkspaceError, match="git_command_failed"):
        create(w)
    with Session(engine) as session:
        row = session.scalar(select(GitWorkspace))
        assert row.state == "FAILED" and row.error_code == "git_command_failed"
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkflowEvent)
                .where(WorkflowEvent.event_type == "workspace.failed")
            )
            == 1
        )
    assert cleanup(w).state == "CLEANED"


def test_creating_recovery_and_missing_directory_cleanup(workspace, engine):
    w = workspace
    info = create(w)
    with Session(engine) as session, session.begin():
        session.execute(update(GitWorkspace).values(state="CREATING"))
    assert create(w).state == "READY"
    # Remove only our registered worktree using manager Git primitive, then retry cleanup.
    w["manager"].git.remove(w["repo"], info.path)
    assert cleanup(w).state == "CLEANED"


def test_root_inside_source_is_rejected_before_write(workspace):
    w = workspace
    candidate = w["repo"] / "generated"
    with pytest.raises(WorkspaceError, match="unsafe_workspace_root"):
        GitWorkspaceManager(w["factory"], candidate, (w["grant"],))
    assert not candidate.exists()


def test_head_tamper_is_rejected(workspace):
    w = workspace
    info = create(w)
    head = w["repo"] / ".git/worktrees" / info.id / "HEAD"
    original = head.read_bytes()
    overwrite_metadata(head, b"ref: refs/heads/main\n")
    try:
        with pytest.raises(WorkspaceError, match="workspace_head_mismatch"):
            export(w)
    finally:
        overwrite_metadata(head, original)


def test_export_detects_concurrent_content_change(workspace, monkeypatch):
    w = workspace
    info = create(w)
    original = w["manager"].git.snapshot
    calls = []

    def changing(path, baseline):
        calls.append(1)
        if len(calls) == 2:
            (info.path / "src/main.txt").write_bytes(b"changed during export\n")
        return original(path, baseline)

    monkeypatch.setattr(w["manager"].git, "snapshot", changing)
    with pytest.raises(WorkspaceError, match="workspace_changed_during_export"):
        export(w)


def test_git_timeout_is_sanitized_and_audited(workspace, engine, monkeypatch):
    w = workspace

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("SECRET_COMMAND", 30, stderr=b"PRIVATE_ERROR")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(WorkspaceError, match="git_timeout") as failure:
        create(w)
    assert "SECRET" not in str(failure.value)
    with Session(engine) as session:
        records = list(
            session.scalars(select(WorkflowEvent).where(WorkflowEvent.source == "git_workspace"))
        )
        assert any(record.payload.get("code") == "git_timeout" for record in records)
        assert "PRIVATE_ERROR" not in str([record.payload for record in records])


def test_independent_prototypes_have_distinct_paths(workspace, api, seed):
    w = workspace
    first = create(w)
    proposal = api.get(f"/api/v1/prototypes/{w['proposal']}", headers=headers()).json()
    post(api, f"prototypes/{w['proposal']}/cancel", {"expected_version": proposal["version"]})
    seed["prototype"].update(base_commit=w["sha"], allowed_paths=["src"])
    second_proposal = post(api, "prototypes", seed).json()
    queued = post(api, f"prototypes/{second_proposal['id']}/start", {"expected_version": 1}).json()
    lease = post(
        api, f"runs/{queued['run_id']}/claim", {"expected_version": queued["version"]}, "worker"
    ).json()
    second = w["manager"].create(w["actor"], queued["run_id"], lease["lease_token"])
    assert first.path != second.path and first.path.exists() and second.path.exists()
    assert (w["repo"] / "README.md").read_bytes() == b"source protected\n"


def test_partial_create_failure_is_tracked_and_cleanable(workspace, engine, monkeypatch):
    w = workspace
    original = w["manager"].git.add

    def partially_created(*args):
        original(*args)
        raise WorkspaceError(409, "injected_after_allocation")

    monkeypatch.setattr(w["manager"].git, "add", partially_created)
    with pytest.raises(WorkspaceError, match="injected_after_allocation"):
        create(w)
    with Session(engine) as session:
        row = session.scalar(select(GitWorkspace))
        assert row.state == "FAILED" and (w["root"] / row.id).exists()
    assert cleanup(w).state == "CLEANED"


def test_case_colliding_directories_rejected():
    seen = {}
    unambiguous("src/Name/a.txt", seen)
    with pytest.raises(WorkspaceError, match="ambiguous_case_path"):
        unambiguous("src/name/b.txt", seen)
