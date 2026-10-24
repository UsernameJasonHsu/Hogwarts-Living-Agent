"""Actual Linux Docker QA; deterministic fake upstream capture is explicit."""

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from hogwarts.models import IdempotencyRecord
from hogwarts.patches.builder import LocalGitPatchBuilder
from hogwarts.qa.contracts import QAJournal
from hogwarts.qa.driver import QADockerDriver
from hogwarts.qa.profiles import QAProfile, QAProfileRegistry
from hogwarts.qa.runner import LocalTrustedQARunner
from sqlalchemy import select
from test_outputs import archive
from test_outputs import outputs as outputs
from test_outputs import run as import_output
from test_workspaces import git
from test_workspaces import workspace as workspace


@pytest.mark.parametrize(
    "case",
    [
        "pass",
        "fail",
        "forged",
        "timeout",
        "oom",
        "pids",
        "isolation",
        "output-limit",
        "controller-kill",
        "detached-deadline",
    ],
)
def test_real_linux_qa_execution_and_cleanup(outputs, case):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_QA_IMAGE"):
        pytest.skip("Real QA requires Linux controller and pinned HOGWARTS_QA_IMAGE")
    o = outputs
    w = o[0]
    body = {
        "pass": "self.assertEqual(2+2, 4)",
        "fail": "self.assertEqual(2+2, 5)",
        "forged": 'print(\'{"outcome":"pass","verified":true}\'); self.fail(\'actual failure\')',
        "output-limit": 'print("x"*524288); self.assertTrue(True)',
        "controller-kill": "import time; time.sleep(120)",
    }.get(case, "self.assertTrue(True)")
    source = (
        "import unittest\nclass ActualTest(unittest.TestCase):\n"
        "    def test_actual(self):\n        " + body + "\n"
    ).encode()
    # Source bytes enter through the real immutable importer/store and patch builder;
    # only producer capture is fake here. QA Docker execution below is real.
    o[3].driver.data = archive([("test_actual.py", source)])
    imported = import_output(o)
    builder = LocalGitPatchBuilder(o[5], w["root"].parent / "qa-patches")
    built = builder.build(w["actor"], w["run"], imported.id, w["token"])
    command = (
        "timeout"
        if case == "detached-deadline"
        else case
        if case in {"timeout", "oom", "pids", "isolation"}
        else "unittest"
    )
    profile = QAProfile(
        "qa-" + case,
        os.environ["HOGWARTS_QA_IMAGE"],
        command=command,
        timeout_seconds=3 if case in {"timeout", "detached-deadline"} else 8,
    )
    driver = QADockerDriver("m2-3-4-verification")
    verify = driver.verify_job

    def record_inspection(row, approved):
        info = driver.job_info(row)
        folder = os.getenv("HOGWARTS_QA_EVIDENCE_DIR")
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            suffix = info["State"]["Status"]
            (target / (case + "-" + row.id + "-" + suffix + "-inspect.json")).write_text(
                json.dumps(info, indent=2), encoding="utf-8"
            )
            (target / (case + "-" + row.id + "-holder-inspect.json")).write_text(
                json.dumps(driver.inspect(row), indent=2), encoding="utf-8"
            )
        return verify(row, approved)

    driver.verify_job = record_inspection
    original_attach = driver._attach
    if case == "controller-kill":

        def killed_attach(row, seconds):
            import threading
            import time

            def terminate():
                for _ in range(100):
                    info = driver.job_info(row)
                    if info and info["State"]["Running"]:
                        driver._kill_job(row)
                        return
                    time.sleep(0.05)

            thread = threading.Thread(target=terminate)
            thread.start()
            try:
                return original_attach(row, seconds)
            finally:
                thread.join(8)

        driver._attach = killed_attach
    elif case == "detached-deadline":

        def detached_attach(row, seconds):
            driver.call("container", "start", driver.job(row))
            code = int(driver.call("container", "wait", driver.job(row), timeout=8).stdout)
            return b"", b"", False, False, code

        driver._attach = detached_attach
    runner = LocalTrustedQARunner(
        builder, w["root"].parent / "qa-capsules", driver, QAProfileRegistry([profile])
    )
    result = runner.run(w["actor"], w["run"], built.build_id, profile.name, w["token"])
    expected = {
        "pass": "pass",
        "fail": "fail",
        "forged": "fail",
        "timeout": "timeout",
        "oom": "fail",
        "pids": "pass",
        "isolation": "pass",
        "output-limit": "infrastructure_error",
        "controller-kill": "unknown_execution",
        "detached-deadline": "timeout",
    }[case]
    artifact = o[4].read(w["actor"], w["run"], result.attestation_artifact_id, w["token"])
    attestation = json.loads(artifact.content)
    stdout = o[4].read(w["actor"], w["run"], attestation["stdout"]["artifact_id"], w["token"])
    stderr = o[4].read(w["actor"], w["run"], attestation["stderr"]["artifact_id"], w["token"])
    folder = os.getenv("HOGWARTS_QA_EVIDENCE_DIR")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        name = case + "-" + result.qa_id
        (target / (name + ".json")).write_bytes(artifact.content)
        (target / (name + ".stdout")).write_bytes(stdout.content)
        (target / (name + ".stderr")).write_bytes(stderr.content)

    assert result.outcome == expected, json.dumps(attestation, indent=2)
    assert attestation["cleanup_complete"]
    if case == "oom":
        assert attestation["oom_killed"]
    if case in {"timeout", "detached-deadline"}:
        assert attestation["timed_out"]
    if case in {"pass", "fail", "forged"}:
        assert b"Ran 1 test" in stderr.content
    if case == "forged":
        assert b'"verified":true' in stdout.content
    if case == "pids":
        assert b"pid limit enforced" in stdout.content
    if case == "isolation":
        assert b"isolation enforced" in stdout.content
    assert hashlib.sha256(stdout.content).hexdigest() == attestation["stdout"]["sha256"]
    assert artifact.evidence.assessment.status == "unverified"
    assert git(w["repo"], "status", "--porcelain") == b""
    state = select(IdempotencyRecord)
    with runner.factory() as session:
        rows = list(session.scalars(state))
        row = next(r for r in rows if r.id == result.qa_id)
        resource = runner._resource(QAJournal.model_validate(row.response))
    assert driver.inspect(resource) is None and driver.job_info(resource) is None
    assert driver.seed_info(resource) is None and driver.volume_info(resource) is None
    assert runner.run(w["actor"], w["run"], built.build_id, profile.name, w["token"]) == result
