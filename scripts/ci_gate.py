"""Exact versioned case/source/JUnit gate; provenance comes from the trusted runner."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = Path("validation/required-cases.v1.json")
IMAGE_VARIABLES = (
    "HOGWARTS_SANDBOX_IMAGE",
    "HOGWARTS_OUTPUT_READER_IMAGE",
    "HOGWARTS_QA_IMAGE",
    "HOGWARTS_REVIEW_QA_IMAGE",
    "HOGWARTS_REVIEW_QA_FAIL_IMAGE",
    "HOGWARTS_REVIEW_QA_ZERO_IMAGE",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fail(code):
    raise ValueError(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            fail("ci_duplicate_json_key")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("ci_json_unavailable") from error


def platform_name():
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "win32":
        return "windows"
    fail("ci_unsupported_platform")


def case_id(value):
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or any(not isinstance(x, str) or not x for x in value)
    ):
        fail("ci_invalid_case_id")
    return tuple(value)


def case_policy(manifest, platform):
    if manifest.get("schema_version") != 1 or platform not in {"linux", "windows"}:
        fail("ci_manifest_version_or_platform")
    values = manifest.get("case_ids", [])
    if not values or len(values) > 10000:
        fail("ci_invalid_required_cases")
    expected = [case_id(x) for x in values]
    if len(set(expected)) != len(expected):
        fail("ci_duplicate_required_case")
    suites = manifest.get("required_suites", [])
    if not suites or any(
        not isinstance(s, str)
        or not s
        or not any(c[0] == s or c[0].startswith(s + ".") for c in expected)
        for s in suites
    ):
        fail("ci_required_suite_absent")
    allowed = {}
    for item in manifest.get("approved_skips", {}).get(platform, []):
        key = case_id(item.get("id"))
        if (
            key not in expected
            or key in allowed
            or not isinstance(item.get("reason"), str)
            or not item["reason"]
        ):
            fail("ci_invalid_skip_policy")
        allowed[key] = item["reason"]
    if platform == "linux" and allowed:
        fail("ci_linux_skip_forbidden")
    return set(expected), allowed


def validate_case_set(records, manifest, platform):
    expected, allowed = case_policy(manifest, platform)
    ids = [case_id([x.get("classname"), x.get("name")]) for x in records]
    if len(set(ids)) != len(ids):
        fail("ci_duplicate_case")
    if set(ids) != expected:
        fail("ci_required_case_set_mismatch")
    skipped = 0
    for key, row in zip(ids, records, strict=True):
        status = row.get("status")
        if status == "skipped":
            if key not in allowed or row.get("skip_reason") != allowed[key]:
                fail("ci_unapproved_skip")
            skipped += 1
        elif status != "passed":
            fail("ci_nonpassing_case")
    return {
        "tests": len(ids),
        "passed": len(ids) - skipped,
        "failed": 0,
        "errors": 0,
        "skipped": skipped,
        "case_set_sha256": digest(sorted(ids)),
    }


def parse_junit(path):
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 16 * 1024 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            fail("ci_unsafe_junit")
        root = ET.fromstring(raw)
    except (OSError, ET.ParseError) as error:
        raise ValueError("ci_junit_unavailable") from error
    if root.tag not in {"testsuites", "testsuite"}:
        fail("ci_invalid_junit_root")
    parent = {child: node for node in root.iter() for child in node}
    if any(
        n.tag in {"failure", "error", "skipped"} and parent[n].tag != "testcase" for n in parent
    ):
        fail("ci_uncontained_failure")
    records = []
    for c in root.iter("testcase"):
        flags = [n for n in c if n.tag in {"failure", "error", "skipped"}]
        if len(flags) > 1:
            fail("ci_ambiguous_case_result")
        flag = flags[0] if flags else None
        records.append(
            {
                "classname": c.get("classname"),
                "name": c.get("name"),
                "status": (
                    {"failure": "failed", "error": "error", "skipped": "skipped"}[flag.tag]
                    if flag is not None
                    else "passed"
                ),
                "skip_reason": flag.get("message", "")
                if flag is not None and flag.tag == "skipped"
                else None,
            }
        )
    if not records:
        fail("ci_empty_junit")
    for suite in root.iter("testsuite"):
        cases = list(suite.iter("testcase"))
        values = {
            "tests": len(cases),
            "failures": sum(c.find("failure") is not None for c in cases),
            "errors": sum(c.find("error") is not None for c in cases),
            "skipped": sum(c.find("skipped") is not None for c in cases),
        }
        for name, value in values.items():
            if name in suite.attrib and suite.get(name) != str(value):
                fail("ci_junit_summary_mismatch")
    return records


def safe_path(root, name):
    if (
        not isinstance(name, str)
        or not name
        or re.search(r'[\\:*?"<>|\x00-\x1f]', name)
        or name.startswith("/")
        or any(p in {"", ".", "..", ".git"} for p in name.split("/"))
    ):
        fail("ci_unsafe_source_path")
    root = Path(root).resolve()
    path = root / name
    if not path.resolve().is_relative_to(root):
        fail("ci_source_escape")
    for p in [path, *path.parents]:
        if p == root:
            break
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            fail("ci_source_link")
    if not path.is_file() or path.stat().st_nlink != 1:
        fail("ci_source_not_regular")
    return path


def source_hash(path):
    with Path(path).open("r", encoding="utf8", newline="") as f:
        data = f.read()
    return hashlib.sha256(data.replace("\r\n", "\n").encode()).hexdigest()


def source_file_set(root):
    root = Path(root)
    if (root / ".git").exists():
        values = []
        for args in [("ls-files", "-z"), ("ls-files", "--others", "--exclude-standard", "-z")]:
            values += (
                subprocess.check_output(["git", "-C", str(root), *args]).decode("utf8").split("\0")
            )
        return set(values) - {""}
    result = set()
    cache = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".next",
        "test-results",
    }
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d not in cache
            and not (
                Path(folder) == root
                and d in {"outputs", "artifacts", "evidence", "secrets", ".secrets"}
            )
        ]
        for name in files:
            if not name.endswith((".pyc", ".pyo")):
                result.add((Path(folder) / name).relative_to(root).as_posix())
    return result


def load_manifest(path):
    m = read_json(path)
    case_policy(m, "linux")
    case_policy(m, "windows")
    if m.get("source_policy") != "utf8-lf-v1" or m.get("purpose") not in {"release", "historical"}:
        fail("ci_invalid_source_policy")
    files = m.get("source_files")
    if (
        not isinstance(files, dict)
        or not files
        or any(not re.fullmatch(r"[0-9a-f]{64}", str(v)) for v in files.values())
        or digest(files) != m.get("source_digest")
    ):
        fail("ci_manifest_source_binding")
    return m


def verify_source(manifest, root):
    for name, expected in manifest["source_files"].items():
        if source_hash(safe_path(root, name)) != expected:
            fail("ci_source_version_mismatch")
    if manifest["purpose"] == "release":
        allowed = read_json(safe_path(root, "validation/commit-allowlist.v1.json"))
        if (
            allowed.get("schema_version") != 1
            or set(allowed.get("paths", []))
            != set(manifest["source_files"]) | {REQUIRED.as_posix()}
            or len(allowed["paths"]) != len(set(allowed["paths"]))
        ):
            fail("ci_allowlist_source_mismatch")
        if source_file_set(root) != set(allowed["paths"]):
            fail("ci_source_file_set_changed")
    return manifest["source_digest"]


def release_identity(manifest_path=None, source_root=None):
    root = Path(source_root or ROOT)
    path = Path(manifest_path or root / REQUIRED)
    m = load_manifest(path)
    if m["purpose"] != "release":
        fail("ci_historical_not_release_authority")
    verify_source(m, root)
    sha = file_sha(path)
    return {
        "source_digest": m["source_digest"],
        "manifest_sha256": sha,
        "release_sha256": digest([m["source_digest"], sha]),
    }


def image_pins(environment):
    pins = {name: environment.get(name, "") for name in IMAGE_VARIABLES}
    if any(not re.fullmatch(r"sha256:[a-f0-9]{64}", v) for v in pins.values()):
        fail("ci_missing_or_unpinned_image")
    return pins


def validate_junit(
    path, *, manifest_path=None, source_root=None, provenance_path=None, platform=None
):
    root = Path(source_root or ROOT)
    mp = Path(manifest_path or root / REQUIRED)
    m = load_manifest(mp)
    identity = release_identity(mp, root)
    stamp = read_json(provenance_path or Path(path).with_name("provenance.json"))
    platform = platform or platform_name()
    if (
        stamp.get("schema_version") != 1
        or stamp.get("platform") != platform
        or any(stamp.get(k) != v for k, v in identity.items())
        or stamp.get("suite_version") != m["suite_version"]
    ):
        fail("ci_provenance_version_mismatch")
    if (
        stamp.get("junit_sha256") != file_sha(path)
        or stamp.get("pytest_exit_code") != 0
        or stamp.get("command_kind") != "full-pytest-v1"
    ):
        fail("ci_provenance_result_mismatch")
    command = stamp.get("command", [])
    if (
        not isinstance(command, list)
        or command[1:3] != ["-m", "pytest"]
        or any(
            x in {"-k", "-m", "--lf", "--ff", "--collect-only", "--ignore", "--deselect"}
            or str(x).startswith(("--ignore=", "--deselect="))
            for x in command[3:]
        )
    ):
        fail("ci_nonfull_command")
    result = validate_case_set(parse_junit(path), m, platform)
    if stamp.get("collection_sha256") != result["case_set_sha256"]:
        fail("ci_collection_result_mismatch")
    if platform == "linux":
        image_pins(stamp.get("images", {}))
    return {
        **result,
        **identity,
        "suite_version": m["suite_version"],
        "platform": platform,
        "scope": "source_bound_local_or_ci_evidence_not_remote_success",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", type=Path)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    if args.images:
        pins = image_pins(os.environ)
        identity = release_identity()
        args.images.parent.mkdir(parents=True, exist_ok=True)
        args.images.write_text(json.dumps({"images": pins, **identity}, indent=2), encoding="utf8")
    if args.source_only:
        print(json.dumps(release_identity()))
    if args.junit:
        print(json.dumps(validate_junit(args.junit)))


if __name__ == "__main__":
    main()
