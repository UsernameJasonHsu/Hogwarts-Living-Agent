"""M2.3.4 contract defined before implementation."""

from dataclasses import FrozenInstanceError

import pytest
from hogwarts.qa.profiles import QAProfile, QAProfileRegistry
from hogwarts.service import DomainError

IMAGE = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    "change",
    [
        {"image": "latest"},
        {"name": "../x"},
        {"command": "sh -c true"},
        {"test_root": "../src"},
        {"test_root": ".ssh"},
        {"cpus": 0},
        {"memory_mb": 1},
        {"pids": 1},
        {"timeout_seconds": 0},
        {"lifetime_seconds": 61},
        {"timeout_seconds": 15, "lifetime_seconds": 8},
    ],
)
def test_invalid_profiles(change):
    args = {"name": "unit", "image": IMAGE, **change}
    with pytest.raises(DomainError):
        QAProfile(**args)


def test_registry_immutable_duplicate_and_no_options():
    profile = QAProfile("unit", IMAGE)
    registry = QAProfileRegistry([profile])
    assert registry.get("unit") == profile
    with pytest.raises(DomainError):
        registry.get("sh")
    with pytest.raises(DomainError):
        QAProfileRegistry([profile, profile])
    with pytest.raises(FrozenInstanceError):
        profile.image = "latest"
    with pytest.raises(TypeError):
        registry.profiles["evil"] = profile
    with pytest.raises(TypeError):
        QAProfile("x", IMAGE, shell="true")
    assert profile.argv[:5] == ("/usr/local/bin/python", "-I", "-B", "-m", "unittest")


def test_versioned_schema_and_false_success_rejected():
    import json
    from pathlib import Path

    from hogwarts.models import now
    from hogwarts.qa.contracts import ExecutionResult, QAExecutionAttestation, QAJournal
    from pydantic import ValidationError

    for filename, model in (("qa-execution", QAExecutionAttestation), ("qa-journal", QAJournal)):
        saved = json.loads(Path("contracts/" + filename + ".v1.schema.json").read_text())
        assert saved == model.model_json_schema()
    args = dict(
        outcome="pass",
        exit_code=1,
        stdout="",
        stderr="",
        started_at=now().isoformat(),
        finished_at=now().isoformat(),
        container_id="a" * 64,
        image_id=IMAGE,
    )
    with pytest.raises(ValidationError):
        ExecutionResult(**args)
    args["exit_code"] = 0
    args["oom_killed"] = True
    with pytest.raises(ValidationError):
        ExecutionResult(**args)
    args["oom_killed"] = False
    args["stdout"] = "not hex"
    with pytest.raises(ValidationError):
        ExecutionResult(**args)


def test_docker_environment_order_is_not_identity_but_duplicates_are_denied():
    from hogwarts.qa.driver import environment_entries

    assert environment_entries(["HOME=/output", "TMPDIR=/tmp"]) == environment_entries(
        ["TMPDIR=/tmp", "HOME=/output"]
    )
    assert environment_entries(["HOME=/output"]) != environment_entries(["HOME=/root"])
    with pytest.raises(DomainError):
        environment_entries(["HOME=/output", "HOME=/root"])
    with pytest.raises(DomainError):
        environment_entries(["invalid"])
