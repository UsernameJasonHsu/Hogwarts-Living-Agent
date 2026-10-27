"""Trusted fixture subprocess resolution, independent of cwd and host PYTHONPATH."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import process_environment as process
import pytest


def relocated(tmp_path, name):
    root = tmp_path / name
    for relative in process.SOURCE_FILES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(process.source_root() / relative, target)
    spec = importlib.util.spec_from_file_location(
        "relocated_process", root / "tests/process_environment.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["runner checkout", "runner-魔法 source"])
def test_relocated_checkout_resolves_current_source(tmp_path, name):
    module = relocated(tmp_path, name)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,process_environment as p; print(json.dumps(p.source_identity()))",
        ],
        cwd=tmp_path,
        env=module.child_environment(),
        capture_output=True,
        check=True,
    )
    observed = json.loads(completed.stdout)
    assert observed["root"] == str(module.source_root())
    assert observed["package"] == str(module.source_root() / "apps/api/hogwarts/__init__.py")
    assert observed["helper"] == str(module.source_root() / "tests/formal_support.py")


def test_inherited_environment_is_not_forwarded(monkeypatch):
    for name in ("PYTHONPATH", "AWS_SECRET_ACCESS_KEY", "TEST_DATABASE_URL", "HTTP_PROXY"):
        monkeypatch.setenv(name, "untrusted-old-checkout-or-secret")
    env = process.child_environment()
    assert env["PYTHONPATH"] == os.pathsep.join(
        str(process.source_root() / p) for p in ("apps/api", "tests")
    )
    assert not any("untrusted" in v for v in env.values())
    assert env["PATH"] == os.defpath
    assert set(env) <= {"PATH", "HOME", "PYTHONPATH", "SYSTEMROOT"}


@pytest.mark.parametrize("relative", ["tests/formal_process.py", "apps/api/hogwarts/__init__.py"])
def test_missing_fixture_source_rejected(tmp_path, relative):
    module = relocated(tmp_path, "incomplete")
    (module.source_root() / relative).unlink()
    with pytest.raises(ValueError, match="invalid_fixture_source"):
        module.child_environment()


def test_linked_fixture_source_rejected(monkeypatch):
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p.name == "formal_process.py" or original(p))
    with pytest.raises(ValueError, match="invalid_fixture_source"):
        process.child_environment()


def test_wrong_import_origin_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        process.importlib.util,
        "find_spec",
        lambda name: SimpleNamespace(origin=str(tmp_path / name)),
    )
    with pytest.raises(ValueError, match="fixture_source_origin_mismatch"):
        process.source_identity()


def test_current_source_identity_has_content_hashes():
    identity = process.source_identity()
    assert identity["root"] == str(Path(__file__).absolute().parents[1])
    assert set(identity["sha256"]) == set(process.SOURCE_FILES)
    assert all(len(value) == 64 for value in identity["sha256"].values())
