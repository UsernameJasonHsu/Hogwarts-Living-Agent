"""Commit/build-context allowlist audit. Never exports matched secret values."""

import argparse
import json
import re
import subprocess
from pathlib import Path

import ci_gate as gate

ROOT_FILES = (
    "README.md",
    "AGENTS.md",
    "CODEX_START_HERE.md",
    "requirements.lock",
    "pyproject.toml",
    "alembic.ini",
    "compose.yaml",
    ".gitignore",
    ".dockerignore",
)
ROOT_DIRS = (
    "apps",
    "workers",
    "packages",
    "infra",
    "tests",
    "contracts",
    "config",
    "docs",
    "scripts",
    "validation",
)
CACHE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    ".next",
    "test-results",
}
DENY_PATTERNS = (
    "**/.git",
    "**/.git/**",
    "**/.venv/**",
    "**/__pycache__/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/node_modules/**",
    "**/.next/**",
    "**/test-results/**",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/id_rsa",
    "**/id_ed25519",
    "**/.ssh/**",
    "**/.aws/**",
    "**/*.private.json",
    "**/state.private.*",
    "**/*.db",
    "**/*.db-*",
    "**/*.sqlite",
    "**/*.sqlite3",
    "**/*.log",
    "**/*.junit.xml",
    "**/*.zip",
    "/outputs/**",
    "/artifacts/**",
    "/evidence/**",
    "/secrets/**",
    "/.secrets/**",
)
SUBCONTEXTS = {
    "infra/sandbox": ("Dockerfile", ".dockerignore", "runner.py"),
    "infra/output-reader": ("Dockerfile", ".dockerignore", "reader.py"),
}


def subcontext_policy(folder):
    return "# RC1 explicit files only.\n**\n" + "".join(
        "!" + name + "\n" for name in SUBCONTEXTS[folder]
    )


def dockerignore_policy(paths):
    if any(any(char in name for char in "[]*?\r\n") for name in paths):
        gate.fail("release_unsafe_context_pattern")
    # Exact file negations let Docker traverse their parents without including
    # arbitrary ignored files under an approved source directory.
    lines = ["# RC1 exact reviewed files only; release_audit.py owns this policy.", "**"]
    lines += ["!" + name for name in sorted(paths)]
    lines += list(DENY_PATTERNS)
    return "\n".join(lines) + "\n"


def denied_path(name):
    parts = Path(name).as_posix().split("/")
    base = parts[-1].lower()
    if "\\" in name or name.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        return True
    if any(p in CACHE_DIRS or p in {".aws", ".ssh"} for p in parts):
        return True
    if parts[0] in {"outputs", "artifacts", "evidence", "secrets", ".secrets"}:
        return True
    return (base.startswith(".env") and base != ".env.example") or bool(
        re.search(
            r"(?i)(\.private\.json$|^state\.private\.|\.(pem|key|p12|pfx|db|sqlite|sqlite3|log|zip)$|\.db-|\.junit\.xml$|^id_(rsa|ed25519)$)",
            base,
        )
    )


def context_allowed(name, paths=None):
    if denied_path(name):
        return False
    if paths is None:
        paths = gate.read_json(gate.ROOT / "validation/commit-allowlist.v1.json")["paths"]
    return name in paths and (
        name in ROOT_FILES
        or name.split("/")[0] in ROOT_DIRS
        or name.startswith(".github/workflows/")
    )


def secret_rules(name, text):
    results = []
    patterns = {
        "private_key": r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
        "provider_token": r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}",
        "github_token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
        "aws_access_key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
        "slack_token": r"\bxox[baprs]-[A-Za-z0-9-]{20,}",
    }
    for number, line in enumerate(text.splitlines(), 1):
        for code, pattern in patterns.items():
            if re.search(pattern, line):
                results.append({"path": name, "line": number, "code": code})
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://([^\s/:\"']+):([^\s@\"']+)@", line):
            password = m.group(2)
            placeholder = (
                "${" in password
                or password in {"PASSWORD", "TEST_PASSWORD"}
                or (password.startswith("<") and password.endswith(">"))
            )
            disposable = name == ".github/workflows/ci.yml" and password == "ci-only"
            if not placeholder and not disposable:
                results.append({"path": name, "line": number, "code": "literal_credential_url"})
    return results


def source_paths(root):
    root = Path(root)
    if (root / ".git").exists():
        values = []
        for args in [("ls-files", "-z"), ("ls-files", "--others", "--exclude-standard", "-z")]:
            values += subprocess.check_output(["git", "-C", str(root), *args]).decode().split("\0")
        return set(values) - {""}
    values = set()
    import os

    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d not in CACHE_DIRS
            and not (
                Path(folder) == root
                and d in {"outputs", "artifacts", "evidence", "secrets", ".secrets"}
            )
        ]
        for name in files:
            rel = (Path(folder) / name).relative_to(root).as_posix()
            if not name.endswith((".pyc", ".pyo")):
                values.add(rel)
    return values


def audit_source(root):
    root = Path(root)
    data = gate.read_json(root / "validation/commit-allowlist.v1.json")
    paths = data.get("paths", [])
    if (
        data.get("schema_version") != 1
        or len(paths) != len(set(paths))
        or set(paths) != source_paths(root)
    ):
        gate.fail("release_commit_allowlist_mismatch")
    findings = []
    for name in paths:
        if denied_path(name) or not context_allowed(name, paths):
            gate.fail("release_private_or_unapproved_path")
        path = gate.safe_path(root, name)
        if path.stat().st_size > 4 * 1024 * 1024:
            gate.fail("release_source_oversize")
        findings += secret_rules(name, path.read_text(encoding="utf8"))
    if findings:
        gate.fail("release_secret_pattern_detected")
    if (root / ".dockerignore").read_text(encoding="utf8").replace(
        "\r\n", "\n"
    ) != dockerignore_policy(paths):
        gate.fail("release_docker_context_policy_drift")
    for folder in SUBCONTEXTS:
        if (root / folder / ".dockerignore").read_text(encoding="utf8").replace(
            "\r\n", "\n"
        ) != subcontext_policy(folder):
            gate.fail("release_subcontext_policy_drift")
    ignores = (root / ".gitignore").read_text(encoding="utf8").splitlines()
    required = {
        ".env.*",
        "*.pem",
        "*.key",
        "*.private.json",
        "/outputs/",
        "/artifacts/",
        "/secrets/",
    }
    if not required <= set(ignores) or any(
        x.startswith("!") and x != "!.env.example" for x in ignores
    ):
        gate.fail("release_ignore_policy_drift")
    return {
        "schema_version": 1,
        "source_files": len(paths),
        "secret_findings": findings,
        "matched_values_exported": False,
        "commit_allowlist_exact": True,
        "docker_policy_exact": True,
        "scope": "eligible_source_heuristics_not_host_or_history_secret_certification",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    result = audit_source(gate.ROOT)
    result.update(gate.release_identity())
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
