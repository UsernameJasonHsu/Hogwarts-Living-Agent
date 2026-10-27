"""Trusted full-suite launcher. No selectors or automatic manifest generation."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import ci_gate as gate
from release_audit import audit_source


def run(output, workers):
    if not 0 <= workers <= 8 or os.getenv("PYTEST_ADDOPTS"):
        gate.fail("ci_external_selection_forbidden")
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    audit_source(gate.ROOT)
    identity = gate.release_identity()
    manifest = gate.load_manifest(gate.ROOT / gate.REQUIRED)
    platform = gate.platform_name()
    images = gate.image_pins(os.environ) if platform == "linux" else {}
    if not os.getenv("TEST_DATABASE_URL"):
        gate.fail("ci_postgresql_not_configured")
    start = datetime.now(UTC).isoformat()
    collect = output / "collection.json"
    command = [sys.executable, "scripts/ci_collect.py", "--output", str(collect)]
    collected = subprocess.run(command, cwd=gate.ROOT, check=False)
    if collected.returncode:
        gate.fail("ci_collection_failed")
    actual = gate.read_json(collect)
    if {tuple(x) for x in actual} != {tuple(x) for x in manifest["case_ids"]}:
        gate.fail("ci_collection_manifest_mismatch")
    junit = output / "junit.xml"
    command = [sys.executable, "-m", "pytest", "-v"]
    if workers:
        command += ["-n", str(workers)]
    command += ["--junitxml=" + str(junit)]
    with (output / "pytest-child.log").open("w", encoding="utf8", newline="\n") as log:
        child = subprocess.Popen(
            command,
            cwd=gate.ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf8",
            errors="replace",
        )
        for line in child.stdout:
            log.write(line)
            log.flush()
            sys.stdout.write(line)
            sys.stdout.flush()
        code = child.wait()
    audit_source(gate.ROOT)
    if gate.release_identity() != identity:
        gate.fail("ci_source_changed_during_tests")
    github = os.getenv("GITHUB_ACTIONS") == "true"
    git_commit = None
    if (gate.ROOT / ".git").exists():
        p = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=gate.ROOT, capture_output=True, text=True, check=True
        )
        git_commit = p.stdout.strip()
    if github and (not git_commit or git_commit != os.getenv("GITHUB_SHA")):
        gate.fail("ci_github_commit_mismatch")
    stamp = {
        "schema_version": 1,
        **identity,
        "suite_version": manifest["suite_version"],
        "platform": platform,
        "command_kind": "full-pytest-v1",
        "command": command,
        "collection_sha256": gate.digest(sorted(actual)),
        "junit_sha256": gate.file_sha(junit),
        "raw_log_sha256": gate.file_sha(output / "pytest-child.log"),
        "pytest_exit_code": code,
        "started_at": start,
        "finished_at": datetime.now(UTC).isoformat(),
        "images": images,
        "execution_environment": "github-actions" if github else "local",
        "git_commit": git_commit,
        "github_run_id": os.getenv("GITHUB_RUN_ID") if github else None,
    }
    (output / "provenance.json").write_text(json.dumps(stamp, indent=2), encoding="utf8")
    result = gate.validate_junit(junit)
    (output / "gate-result.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result))
    return code


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("test-results"))
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args()
    try:
        raise SystemExit(run(a.output, a.workers))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
