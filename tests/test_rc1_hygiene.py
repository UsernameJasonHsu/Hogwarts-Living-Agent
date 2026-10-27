import importlib.util
from pathlib import Path

import pytest


def audit():
    spec = importlib.util.spec_from_file_location("release_audit", Path("scripts/release_audit.py"))
    m = importlib.util.module_from_spec(spec)
    import sys

    sys.path.insert(0, str(Path("scripts").resolve()))
    try:
        spec.loader.exec_module(m)
    finally:
        sys.path.pop(0)
    return m


@pytest.mark.parametrize(
    "name",
    [
        ".env.production",
        ".env.local",
        "keys/release.pem",
        "apps/api/.env",
        "config/key.pfx",
        "state.private.json",
        "outputs/review.json",
        "artifacts/result.zip",
        "test-results/private.json",
        ".ssh/id_rsa",
        ".aws/credentials",
        "__pycache__/code.pyc",
        "../escape.py",
    ],
)
def test_rc1_private_paths_not_commit_or_context(name):
    m = audit()
    assert m.denied_path(name) and not m.context_allowed(name)


@pytest.mark.parametrize(
    "name",
    [
        "apps/api/hogwarts/outputs/importer.py",
        "apps/api/hogwarts/artifacts/store.py",
        "scripts/ci_gate.py",
        "validation/required-cases.v1.json",
        ".github/workflows/ci.yml",
    ],
)
def test_rc1_product_paths_stay_in_context(name):
    assert audit().context_allowed(name)


@pytest.mark.parametrize("kind", ["private", "provider", "github", "aws", "url"])
def test_rc1_secret_detector_redacts_values(kind):
    samples = {
        "private": "-----BEGIN " + "PRIVATE KEY-----",
        "provider": "sk-proj-" + "a" * 48,
        "github": "ghp_" + "a" * 40,
        "aws": "AKIA" + "A" * 16,
        "url": "postgresql://account:" + "actual-secret-value@host/db",
    }
    result = audit().secret_rules("apps/example.py", samples[kind])
    assert result
    assert samples[kind] not in repr(result)


def test_rc1_existing_disposable_templates_not_secrets():
    m = audit()
    assert not m.secret_rules(
        "compose.yaml", "DATABASE_URL=postgresql://user:${POSTGRES_PASSWORD}@postgres/db"
    )
    sample = "postgresql://hogwarts:" + "ci-only@localhost/db"
    assert not m.secret_rules(".github/workflows/ci.yml", sample)
    assert m.secret_rules("apps/app.py", sample)


def test_rc1_unlisted_filename_is_not_context_approved():
    m = audit()
    name = "apps/api/unlisted-config.json"
    assert not m.denied_path(name)  # Generic name, not caught by secret filename rules.
    assert not m.context_allowed(name)  # Exact reviewed-file policy still excludes it.
    with pytest.raises(ValueError, match="release_unsafe_context_pattern"):
        m.dockerignore_policy(["apps/api/[unsafe].json"])
