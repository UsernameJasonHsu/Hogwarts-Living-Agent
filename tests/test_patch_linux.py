"""Real Linux sandbox/import/Git integration, with no QA or project command execution."""

import hashlib

import pytest
from hogwarts.patches.builder import LocalGitPatchBuilder
from test_output_linux import linux_output as linux_output
from test_workspaces import git
from test_workspaces import workspace as workspace


@pytest.mark.parametrize("destroy_first", [False, True])
def test_real_linux_import_to_binary_capable_patch_builder(linux_output, destroy_first):
    o = linux_output
    w, ws, sb, c, store, importer, _ = o
    c.execute(w["actor"], w["run"], w["token"], "patch-input")
    imported = c.import_outputs(importer, w["actor"], w["run"], ws.id, sb.id, w["token"])
    if destroy_first:
        c.destroy(w["actor"], w["run"], w["token"])
    b = LocalGitPatchBuilder(importer, w["root"].parent / "trusted-patch-root")
    result = b.build(w["actor"], w["run"], imported.id, w["token"])
    patch = store.read(w["actor"], w["run"], result.patch_artifact_id, w["token"])
    assert result.changed_paths == ["src/prototype.txt"]
    assert result.assessment == patch.evidence.assessment.status == "unverified"
    assert result.applicability == "applicable"
    assert hashlib.sha256(patch.content).hexdigest() == result.patch_sha256
    assert b"+deterministic prototype\n" in patch.content
    assert git(w["repo"], "status", "--porcelain") == b""
    assert not (ws.path / "src/prototype.txt").exists()
    assert b.build(w["actor"], w["run"], imported.id, w["token"]) == result
