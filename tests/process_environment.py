"""Test-only trusted checkout binding; never supplied by Agent input or fixture JSON."""

import hashlib
import importlib.util
import os
import stat
from pathlib import Path

SOURCE_FILES = (
    "pyproject.toml",
    "apps/api/hogwarts/__init__.py",
    "apps/api/hogwarts/entrypoints/gateway.py",
    "tests/process_environment.py",
    "tests/formal_process.py",
    "tests/formal_support.py",
)


def source_root():
    return Path(__file__).absolute().parents[1]


def source_paths():
    root = source_root()
    paths = {}
    for relative in SOURCE_FILES:
        path = root / relative
        try:
            if any(p.is_symlink() for p in [path, *path.parents] if p != root):
                raise ValueError("invalid_fixture_source")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("invalid_fixture_source")
        except OSError as error:
            raise ValueError("invalid_fixture_source") from error
        paths[relative] = path
    return paths


def child_environment():
    source_paths()
    root = source_root()
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "PYTHONPATH": os.pathsep.join(str(root / p) for p in ("apps/api", "tests")),
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def source_identity():
    paths = source_paths()
    modules = {
        "hogwarts": "apps/api/hogwarts/__init__.py",
        "formal_support": "tests/formal_support.py",
    }
    for name, relative in modules.items():
        spec = importlib.util.find_spec(name)
        if spec is None or not spec.origin or Path(spec.origin).absolute() != paths[relative]:
            raise ValueError("fixture_source_origin_mismatch")
    return {
        "root": str(source_root()),
        "package": str(paths[modules["hogwarts"]]),
        "helper": str(paths[modules["formal_support"]]),
        "sha256": {name: hashlib.sha256(p.read_bytes()).hexdigest() for name, p in paths.items()},
    }
