"""RC-FIX-01 contracts: exact IDs and source-bound original JUnit."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest


def gate():
    spec = importlib.util.spec_from_file_location("rc1_ci_gate", Path("scripts/ci_gate.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def manifest():
    return {
        "schema_version": 1,
        "suite_version": "contract",
        "case_ids": [["suite", "one"], ["suite", "two"]],
        "required_suites": ["suite"],
        "approved_skips": {
            "linux": [],
            "windows": [{"id": ["suite", "two"], "reason": "Linux only"}],
        },
    }


def records():
    return [
        {"classname": "suite", "name": n, "status": "passed", "skip_reason": None}
        for n in ("one", "two")
    ]


def test_rc1_exact_case_set_passes():
    assert gate().validate_case_set(records(), manifest(), "linux")["tests"] == 2


@pytest.mark.parametrize(
    "attack", ["missing", "duplicate", "extra", "failed", "error", "skip", "reason", "platform"]
)
def test_rc1_case_set_rejects_incomplete_or_nonpassing(attack):
    data = records()
    m = manifest()
    platform = "linux"
    if attack == "missing":
        data.pop()
    elif attack == "duplicate":
        data.append(copy.deepcopy(data[0]))
    elif attack == "extra":
        data.append({**data[0], "name": "extra"})
    elif attack in {"failed", "error"}:
        data[0]["status"] = attack
    elif attack == "skip":
        data[0].update(status="skipped", skip_reason="arbitrary")
    elif attack == "reason":
        platform = "windows"
        data[1].update(status="skipped", skip_reason="DB unavailable")
    else:
        platform = "darwin"
    with pytest.raises(ValueError):
        gate().validate_case_set(data, m, platform)


def test_rc1_windows_skip_is_exactly_approved():
    data = records()
    data[1].update(status="skipped", skip_reason="Linux only")
    assert gate().validate_case_set(data, manifest(), "windows")["skipped"] == 1
    with pytest.raises(ValueError):
        gate().validate_case_set(data, manifest(), "linux")


@pytest.mark.parametrize(
    "payload", ["<!DOCTYPE x><testsuites/>", "<!ENTITY x 'x'><testsuites/>", "<bad/>"]
)
def test_rc1_untrusted_junit_structure_rejected(tmp_path, payload):
    path = tmp_path / "bad.xml"
    path.write_text(payload, encoding="utf8")
    with pytest.raises(ValueError):
        gate().parse_junit(path)


def test_rc1_absent_provenance_cannot_pass(tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text(
        '<testsuites><testsuite><testcase classname="suite" name="one"/></testsuite></testsuites>',
        encoding="utf8",
    )
    with pytest.raises(ValueError):
        gate().validate_junit(p)


def release_fixture(root):
    g = gate()
    (root / "validation").mkdir()
    (root / "test-results").mkdir()
    (root / "code.py").write_text("print('safe')\n", encoding="utf8")
    allowed = {
        "schema_version": 1,
        "paths": [
            "code.py",
            "validation/commit-allowlist.v1.json",
            "validation/required-cases.v1.json",
        ],
    }
    (root / "validation/commit-allowlist.v1.json").write_text(json.dumps(allowed), encoding="utf8")
    m = manifest()
    m.update(purpose="release", source_policy="utf8-lf-v1")
    m["source_files"] = {
        p: g.source_hash(root / p)
        for p in allowed["paths"]
        if p != "validation/required-cases.v1.json"
    }
    m["source_digest"] = g.digest(m["source_files"])
    mp = root / "validation/required-cases.v1.json"
    mp.write_text(json.dumps(m), encoding="utf8")
    jp = root / "test-results/junit.xml"
    jp.write_text(
        '<testsuites><testsuite tests="2" errors="0" failures="0" skipped="0">'
        '<testcase classname="suite" name="one"/>'
        '<testcase classname="suite" name="two"/></testsuite></testsuites>',
        encoding="utf8",
    )
    stamp = {
        "schema_version": 1,
        **g.release_identity(mp, root),
        "platform": "linux",
        "suite_version": "contract",
        "junit_sha256": g.file_sha(jp),
        "pytest_exit_code": 0,
        "command_kind": "full-pytest-v1",
        "command": ["python", "-m", "pytest", "-v"],
        "collection_sha256": g.digest(sorted(m["case_ids"])),
        "images": {n: "sha256:" + "a" * 64 for n in g.IMAGE_VARIABLES},
    }
    sp = root / "test-results/provenance.json"
    sp.write_text(json.dumps(stamp), encoding="utf8")
    return g, mp, jp, sp, stamp


def test_rc1_original_junit_is_source_and_provenance_bound(tmp_path):
    g, mp, jp, sp, _ = release_fixture(tmp_path)
    assert (
        g.validate_junit(
            jp, manifest_path=mp, source_root=tmp_path, provenance_path=sp, platform="linux"
        )["tests"]
        == 2
    )


@pytest.mark.parametrize(
    "part",
    [
        "source",
        "extra_source",
        "manifest",
        "release",
        "junit",
        "collection",
        "platform",
        "version",
        "exit",
        "selector",
        "image",
    ],
)
def test_rc1_source_or_provenance_mismatch_denied(tmp_path, part):
    g, mp, jp, sp, stamp = release_fixture(tmp_path)
    if part == "source":
        (tmp_path / "code.py").write_text("print('changed')\n", encoding="utf8")
    elif part == "extra_source":
        (tmp_path / "extra.py").write_text("unsafe=1\n", encoding="utf8")
    elif part == "junit":
        jp.write_bytes(jp.read_bytes() + b"\n")
    else:
        if part == "manifest":
            stamp["manifest_sha256"] = "0" * 64
        elif part == "release":
            stamp["release_sha256"] = "0" * 64
        elif part == "collection":
            stamp["collection_sha256"] = "0" * 64
        elif part == "platform":
            stamp["platform"] = "windows"
        elif part == "version":
            stamp["suite_version"] = "other"
        elif part == "exit":
            stamp["pytest_exit_code"] = 1
        elif part == "selector":
            stamp["command"] += ["-k", "subset"]
        else:
            stamp["images"].pop(g.IMAGE_VARIABLES[0])
        sp.write_text(json.dumps(stamp), encoding="utf8")
    with pytest.raises(ValueError):
        g.validate_junit(
            jp, manifest_path=mp, source_root=tmp_path, provenance_path=sp, platform="linux"
        )


def test_rc1_historical_107_case_removal_is_rejected():
    g = gate()
    m = g.load_manifest(Path("validation/history/m2-5-required-cases.v1.json"))
    rows = [
        {"classname": c, "name": n, "status": "passed", "skip_reason": None}
        for c, n in m["case_ids"]
    ]
    assert g.validate_case_set(rows, m, "linux")["tests"] == 1471
    reduced = [row for row in rows if not row["classname"].startswith("tests.test_formal")]
    assert len(rows) - len(reduced) == 107
    with pytest.raises(ValueError, match="ci_required_case_set_mismatch"):
        g.validate_case_set(reduced, m, "linux")
    current = g.load_manifest(Path("validation/required-cases.v1.json"))
    assert set(map(tuple, m["case_ids"])) <= set(map(tuple, current["case_ids"]))
