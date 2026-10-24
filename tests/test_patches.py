"""Preimplementation contract: real private Git builds over actual immutable imports."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from hogwarts.models import ArtifactMetadata, IdempotencyRecord, OutputImport, PrototypeRun, now
from hogwarts.patches.builder import LocalGitPatchBuilder
from hogwarts.patches.contracts import BuildJournal, PatchError
from hogwarts.service import DomainError
from sqlalchemy import func, select
from test_outputs import archive
from test_outputs import outputs as outputs
from test_outputs import run as import_outputs
from test_workspaces import git
from test_workspaces import workspace as workspace


@pytest.fixture
def patches(outputs):
    o = outputs
    o[3].driver.data = archive([("main.txt", b"after\n"), ("new.txt", b"new\n")])
    imported = import_outputs(o)
    builder = LocalGitPatchBuilder(o[5], o[0]["root"].parent / "patch-builder")
    return o, imported, builder


def build(p, **changes):
    o, imported, builder = p
    args = dict(
        actor=o[0]["actor"], run_id=o[0]["run"], import_id=imported.id, lease_token=o[0]["token"]
    )
    args.update(changes)
    return builder.build(**args)


def read(p, identifier):
    w = p[0][0]
    return p[0][4].read(w["actor"], w["run"], identifier, w["token"])


def journal(p):
    with p[0][0]["factory"]() as session:
        row = session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key.like("patch-builder.v1:%"))
        )
        return row, BuildJournal.model_validate(row.response)


def test_text_add_modify_source_unchanged_and_apply_is_not_qa(patches):
    p = patches
    o, imported, b = p
    source_index = (o[0]["repo"] / ".git/index").read_bytes()
    result = build(p)
    content = read(p, result.patch_artifact_id)
    assert result.changed_paths == ["src/main.txt", "src/new.txt"]
    assert result.applicability == "applicable" and result.assessment == "unverified"
    assert (
        content.evidence.artifact.kind == "patch"
        and content.evidence.assessment.status == "unverified"
    )
    assert b"after" in content.content and b"new file mode 100644" in content.content
    assert b"deleted file mode" not in content.content
    assert git(o[0]["repo"], "status", "--porcelain") == b""
    assert (o[0]["repo"] / ".git/index").read_bytes() == source_index
    assert (o[1].path / "src/main.txt").read_bytes() == b"before\n"
    assert git(o[0]["repo"], "rev-parse", "HEAD").decode().strip() == o[0]["sha"]
    assert build(p) == result
    other = LocalGitPatchBuilder(o[5], b.objects.root)
    assert other.build(o[0]["actor"], o[0]["run"], imported.id, o[0]["token"]) == result


@pytest.mark.parametrize("kind", ["binary", "empty", "unchanged", "rename_like"])
def test_binary_empty_and_no_deletion_or_rename_inference(outputs, kind):
    o = outputs
    entries = {
        "binary": [("data.bin", b"\x00new\xff\x00")],
        "empty": [],
        "unchanged": [("main.txt", b"before\n")],
        "rename_like": [("renamed.txt", b"before\n")],
    }[kind]
    o[3].driver.data = archive(entries)
    imported = import_outputs(o)
    p = (o, imported, LocalGitPatchBuilder(o[5], o[0]["root"].parent / "patch-builder"))
    result = build(p)
    content = read(p, result.patch_artifact_id).content
    if kind == "binary":
        assert b"GIT binary patch" in content
    if kind in {"empty", "unchanged"}:
        assert (
            content == b""
            and result.changed_paths == []
            and result.applicability == "empty_no_changes"
        )
    if kind == "rename_like":
        assert result.changed_paths == ["src/renamed.txt"]
        assert b"rename from" not in content and b"deleted file" not in content


@pytest.mark.parametrize(
    "kind", ["owner", "worker", "human", "run", "import", "token", "expired", "cancelled", "grant"]
)
def test_binding_denials(patches, kind):
    p = patches
    w = p[0][0]
    args = {}
    if kind == "owner":
        args["actor"] = replace(w["actor"], owner_id="other")
    if kind == "worker":
        args["actor"] = replace(w["actor"], id="worker:other")
    if kind == "human":
        args["actor"] = replace(w["actor"], role="human")
    if kind in {"run", "import"}:
        args[kind + "_id"] = str(uuid4())
    if kind == "token":
        args["lease_token"] = "wrong"
    if kind in {"expired", "cancelled"}:
        with w["factory"].begin() as session:
            row = session.get(PrototypeRun, w["run"])
            if kind == "expired":
                row.lease_expires_at = now() - timedelta(seconds=1)
            else:
                row.status = "CANCELLED"
    if kind == "grant":
        w["manager"].grants.clear()
    with pytest.raises(DomainError):
        build(p, **args)
    with w["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ArtifactMetadata)) == 4


def test_concurrent_independent_builders(patches):
    p = patches
    o, imported, b = p
    w = o[0]
    builders = [LocalGitPatchBuilder(o[5], b.objects.root) for _ in range(4)]

    def operation(index):
        return builders[index].build(w["actor"], w["run"], imported.id, w["token"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(operation, range(4)))
    assert all(x == results[0] for x in results)
    with w["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ArtifactMetadata)) == 6


@pytest.mark.parametrize(
    "name", ["ignored.txt", "sub/space name.txt", "-leading.txt", "中文.txt", "name; echo injected"]
)
def test_portable_explicit_paths_and_ignored_files(outputs, name):
    o = outputs
    o[3].driver.data = archive([(name, b"content\n")])
    p = (o, import_outputs(o), LocalGitPatchBuilder(o[5], o[0]["root"].parent / "patch-builder"))
    assert build(p).changed_paths == ["src/" + name]
    assert not (o[0]["repo"] / "injected").exists()


@pytest.mark.parametrize(
    "kind",
    ["manifest_hash", "file_hash", "capture_proof", "base_commit", "not_imported", "workspace"],
)
def test_import_or_base_metadata_mismatch(patches, kind):
    p = patches
    w = p[0][0]
    with w["factory"].begin() as session:
        row = session.get(OutputImport, p[1].id)
        if kind == "manifest_hash":
            row.manifest_sha256 = "0" * 64
        elif kind == "file_hash":
            row.files = [{**row.files[0], "sha256": "0" * 64}, *row.files[1:]]
        elif kind == "capture_proof":
            row.proof = {**row.proof, "container_id": "other"}
        elif kind == "not_imported":
            row.state = "IMPORTING"
        elif kind == "base_commit":
            from hogwarts.models import GitWorkspace

            session.get(GitWorkspace, row.workspace_id).base_commit = "a" * 40
        else:
            from hogwarts.models import GitWorkspace

            session.get(GitWorkspace, row.workspace_id).state = "CLEANED"
    with pytest.raises(DomainError):
        build(p)
    # No patch artifact has been published.
    with w["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(ArtifactMetadata)) == 4


@pytest.mark.parametrize("kind", ["source_file", "manifest", "archive"])
def test_source_artifact_tampering(patches, kind):
    p = patches
    o, _, _ = p
    with o[0]["factory"]() as session:
        row = session.get(OutputImport, p[1].id)
        if kind == "manifest":
            identifier = row.manifest_artifact_id
        elif kind == "source_file":
            identifier = row.files[0]["artifact_id"]
        else:
            meta = session.scalar(
                select(ArtifactMetadata).where(ArtifactMetadata.sha256 == row.archive_sha256)
            )
            identifier = meta.id
    path = o[4].objects.path(identifier)
    path.chmod(0o600)
    with path.open("r+b") as stream:
        stream.write(b"corrupt")
    with pytest.raises(DomainError):
        build(p)


@pytest.mark.parametrize("extra", ["mode", "delete", "rename", "host_path", "verified"])
def test_manifest_contract_rejects_extra_fields(extra):
    from hogwarts.patches.contracts import ImportedFile
    from pydantic import ValidationError

    record = {
        "source_path": "a",
        "target_path": "src/a",
        "sha256": "a" * 64,
        "size_bytes": 1,
        "artifact_id": str(uuid4()),
        extra: "forged",
    }
    with pytest.raises(ValidationError):
        ImportedFile.model_validate(record)


@pytest.mark.parametrize("kind", ["file_content", "added_file", "hardlink", "symlink", "fifo"])
def test_private_reconstruction_toctou_is_rejected(patches, monkeypatch, kind):
    import os

    if kind in {"symlink", "fifo"} and os.name == "nt":
        pytest.skip("Native link/FIFO probe runs on Linux")
    p = patches
    b = p[2]
    original = b.git.snapshot
    calls = 0
    sentinel = p[0][0]["root"].parent / "outside-sentinel"
    sentinel.write_bytes(b"outside")

    def altered(work, expected):
        nonlocal calls
        calls += 1
        if calls == 2:
            target = work / "src/main.txt"
            if kind == "file_content":
                target.write_bytes(b"tampered")
            elif kind == "added_file":
                (work / "outside.txt").write_bytes(b"new")
            elif kind == "hardlink":
                os.link(target, work / "alias")
            elif kind == "symlink":
                target.unlink()
                target.symlink_to(sentinel)
            else:
                target.unlink()
                os.mkfifo(target)
        return original(work, expected)

    monkeypatch.setattr(b.git, "snapshot", altered)
    with pytest.raises(DomainError):
        build(p)
    assert sentinel.read_bytes() == b"outside"
    assert journal(p)[1].state == "FAILED"
    assert not list(b.objects.root.glob("build-*"))


@pytest.mark.parametrize("change", ["name_status", "diff", "apply_content"])
def test_git_output_or_apply_mismatch_rejected(patches, monkeypatch, change):
    b = patches[2]
    original = b.git.invoke

    def altered(scratch, *args, **kwargs):
        result = original(scratch, *args, **kwargs)
        if args and args[0] == "diff":
            if change == "name_status" and "--name-status" in args:
                return b"M\0README.md\0"
            if "--binary" in args:
                if change == "diff":
                    return b""
                if change == "apply_content":
                    return result.replace(b"+after\n", b"+other\n")
        return result

    monkeypatch.setattr(b.git, "invoke", altered)
    with pytest.raises(DomainError):
        build(patches)
    assert journal(patches)[1].state == "FAILED"


@pytest.mark.parametrize("failure", ["storage", "database", "candidate_commit", "final_commit"])
def test_durable_commit_and_publication_retries(patches, monkeypatch, failure):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    p = patches
    b = p[2]
    original = b.store.put
    fired = False

    def broken(*args, **kwargs):
        nonlocal fired
        if not fired:
            fired = True
            if failure == "storage":
                raise OSError("injected")
            raise OperationalError("injected", {}, Exception())
        return original(*args, **kwargs)

    desired = "BUILT" if failure == "candidate_commit" else "READY"

    def fail_commit(session):
        nonlocal fired
        if not fired and any(
            isinstance(x, IdempotencyRecord)
            and x.key.startswith("patch-builder.v1:")
            and x.response.get("state") == desired
            for x in session.dirty
        ):
            fired = True
            raise OperationalError("injected", {}, Exception())

    if failure in {"candidate_commit", "final_commit"}:
        event.listen(Session, "before_commit", fail_commit)
    else:
        monkeypatch.setattr(b.store, "put", broken)
    try:
        with pytest.raises(PatchError):
            build(p)
    finally:
        if failure in {"candidate_commit", "final_commit"}:
            event.remove(Session, "before_commit", fail_commit)
        monkeypatch.setattr(b.store, "put", original)
    result = build(p)
    assert result.assessment == "unverified" and journal(p)[1].state == "READY"


@pytest.mark.parametrize(
    "kind", ["candidate", "source_during_build", "revoke_during_build", "proof"]
)
def test_tampering_or_revocation_never_returns_success(patches, monkeypatch, kind):
    p = patches
    b = p[2]
    if kind in {"candidate", "proof"}:
        build(p)
        row, state = journal(p)
        if kind == "candidate":
            path = b.objects.path(state.build_id)
            path.chmod(0o600)
            with path.open("r+b") as stream:
                stream.write(b"changed")
        else:
            with p[0][0]["factory"].begin() as session:
                record = session.get(IdempotencyRecord, row.id)
                record.response = {**record.response, "proof_sha256": "0" * 64}
    else:
        original = b.git.construct

        def modified(inputs):
            result = original(inputs)
            if kind == "source_during_build":
                path = b.store.objects.path(inputs.manifest.files[0].artifact_id)
                path.chmod(0o600)
                with path.open("r+b") as stream:
                    stream.write(b"changed")
            else:
                b.workspaces.grants.clear()
            return result

        monkeypatch.setattr(b.git, "construct", modified)
    with pytest.raises(DomainError):
        build(p)


def test_no_agent_host_path_or_shell_options(patches):
    for field in ["host_path", "patch", "base_commit", "command", "git_args"]:
        with pytest.raises(TypeError):
            build(patches, **{field: "../../outside; echo unsafe"})


def test_unavailable_db_and_audit_reported(patches, monkeypatch):
    from sqlalchemy.exc import OperationalError

    class Unavailable:
        def begin(self):
            raise OperationalError("injected", {}, Exception())

    monkeypatch.setattr(patches[2], "factory", Unavailable())
    with pytest.raises(PatchError, match="patch_database_and_audit_unavailable"):
        build(patches)


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute", "src/../../bad", "src/.git/config", "src/main.txt/child", "src"],
)
def test_core_defence_in_depth_rejects_traversal(patches, path):
    from dataclasses import replace

    o, imported, b = patches
    w = o[0]
    inputs = b.validator.load(w["actor"], w["run"], imported.id, w["token"])
    unsafe = replace(inputs, contents={path: b"bad"})
    with pytest.raises(DomainError):
        b.git.construct(unsafe)
    assert not list(b.objects.root.glob("build-*"))


@pytest.mark.parametrize("kind", ["symlink", "submodule", "executable"])
def test_git_tree_type_and_mode_contract(patches, kind):
    from dataclasses import replace

    o, imported, b = patches
    w = o[0]
    inputs = b.validator.load(w["actor"], w["run"], imported.id, w["token"])
    if kind == "symlink":
        sha = git(w["repo"], "hash-object", "-w", "--stdin", data=b"../../outside").decode().strip()
        git(w["repo"], "update-index", "--add", "--cacheinfo", "120000," + sha + ",src/link")
    elif kind == "submodule":
        git(w["repo"], "update-index", "--add", "--cacheinfo", "160000," + w["sha"] + ",src/module")
    else:
        git(w["repo"], "update-index", "--chmod=+x", "src/main.txt")
    git(w["repo"], "commit", "-m", "test-only mode/type fixture")
    sha = git(w["repo"], "rev-parse", "HEAD").decode().strip()
    changed = replace(inputs, context=replace(inputs.context, base_commit=sha))
    if kind != "executable":
        with pytest.raises(DomainError, match="unsafe_git_tree"):
            b.git.construct(changed)
    else:
        patch, proof = b.git.construct(changed)
        assert b"old mode" not in patch and b"new mode" not in patch
        assert b"100755" in patch and proof.applicability == "applicable"


def test_inherited_git_execution_configuration_is_ignored(patches, monkeypatch):
    w = patches[0][0]
    sentinel = w["root"].parent / "must-not-execute"
    script = w["root"].parent / "hostile-diff.sh"
    script.write_text('#!/bin/sh\nprintf unsafe > "' + sentinel.as_posix() + '"\n')
    script.chmod(0o700)
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(script))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(script.parent))
    monkeypatch.setenv("GIT_SSH_COMMAND", "should-never-run")
    assert build(patches).assessment == "unverified"
    assert not sentinel.exists()


def test_partial_candidate_never_overwritten(patches, monkeypatch):
    b = patches[2]

    def partial(identifier, content, sha):
        path = b.objects.path(identifier)
        path.write_bytes(content[:1])
        raise OSError("injected partial candidate")

    monkeypatch.setattr(b.objects, "publish", partial)
    with pytest.raises(PatchError):
        build(patches)
    state = journal(patches)[1]
    assert state.state == "FAILED"
    path = b.objects.path(state.build_id)
    before = path.read_bytes()
    with pytest.raises(PatchError):
        build(patches)
    assert path.read_bytes() == before


def test_schema_snapshot_matches_typed_journal():
    from pathlib import Path

    assert (
        json.loads(Path("contracts/patch-build.v1.schema.json").read_text())
        == BuildJournal.model_json_schema()
    )


def test_existing_journal_namespace_collision_fails_closed(patches):
    p = patches
    w = p[0][0]
    with w["factory"].begin() as session:
        session.add(
            IdempotencyRecord(
                owner_id=w["actor"].owner_id,
                actor_id=w["actor"].id,
                key="patch-builder.v1:" + p[1].id,
                request_hash="0" * 64,
                response={"foreign": "untouched"},
            )
        )
    with pytest.raises(PatchError, match="patch_request_conflict"):
        build(p)
    with w["factory"]() as session:
        record = session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.key.like("patch-builder.v1:%"))
        )
        assert record.response == {"foreign": "untouched"}


def test_durable_audit_and_existing_run_evidence_unchanged(patches):
    from hogwarts.models import WorkflowEvent

    p = patches
    w = p[0][0]
    with w["factory"]() as session:
        before = session.get(PrototypeRun, w["run"]).evidence
    result = build(p)
    with w["factory"]() as session:
        events = list(
            session.scalars(
                select(WorkflowEvent)
                .where(WorkflowEvent.source == "trusted_git_patch_builder")
                .order_by(WorkflowEvent.occurred_at)
            )
        )
        assert [e.event_type for e in events] == [
            "patch_build.pending",
            "patch_build.building",
            "patch_build.built",
            "patch_build.publishing",
            "patch_build.ready",
        ]
        assert w["token"] not in str([e.payload for e in events])
        assert session.get(PrototypeRun, w["run"]).evidence == before
    proof = json.loads(read(p, result.proof_artifact_id).content)
    assert (
        proof["assessment"] == "unverified" and "qa_passed" not in proof and "verified" not in proof
    )


def test_oversized_base_blob_rejected_before_content_read(patches, monkeypatch):
    from dataclasses import replace

    o, imported, b = patches
    w = o[0]
    inputs = b.validator.load(w["actor"], w["run"], imported.id, w["token"])
    sha = (
        git(w["repo"], "hash-object", "-w", "--stdin", data=b"x" * (8 * 1024 * 1024 + 1))
        .decode()
        .strip()
    )
    git(w["repo"], "update-index", "--cacheinfo", "100644," + sha + ",src/main.txt")
    git(w["repo"], "commit", "-m", "test-only oversized base")
    commit = git(w["repo"], "rev-parse", "HEAD").decode().strip()
    original = b.git.source_git.run
    reads = []

    def observed(repo, *args, **kwargs):
        if args[:2] == ("cat-file", "blob"):
            reads.append(args[2])
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(b.git.source_git, "run", observed)
    unsafe = replace(inputs, context=replace(inputs.context, base_commit=commit))
    with pytest.raises(PatchError, match="patch_base_blob_limit"):
        b.git.construct(unsafe)
    assert sha not in reads
