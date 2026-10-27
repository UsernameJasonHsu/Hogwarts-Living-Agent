"""M2.3.5 test contracts defined before implementation."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from hogwarts.qa.profiles import QAProfile
from hogwarts.service import DomainError
from hogwarts.verification.contracts import ReviewBundle, VerificationJournal
from hogwarts.verification.profiles import ReviewQAProfile, approved_review_profile
from pydantic import ValidationError

IMAGE = "sha256:" + "a" * 64


def test_legacy_zero_test_success_is_insufficient():
    with pytest.raises(DomainError):
        approved_review_profile(QAProfile("legacy", IMAGE))


def test_review_profile_fixed_command_and_positive_test_contract():
    p = ReviewQAProfile("review", IMAGE)
    approved_review_profile(p)
    assert p.command == "acceptance" and p.expected_tests == 2
    assert p.argv[-1] == "src"
    assert "review-tests.py" in p.argv[-2]
    assert p.fingerprint == ReviewQAProfile("review", IMAGE).fingerprint


@pytest.mark.parametrize("bad", ["../escape", "/host", ".ssh", "src/../etc"])
def test_review_root_validation(bad):
    with pytest.raises((DomainError, ValueError)):
        ReviewQAProfile("review", IMAGE, test_root=bad)


@pytest.mark.parametrize("changes", [{"image": "latest"}, {"command": "shell"}, {"pids": 0}])
def test_review_profile_rejects_unsafe_configuration(changes):
    with pytest.raises(DomainError):
        replace(ReviewQAProfile("review", IMAGE), **changes)


def test_bundle_cannot_be_an_agent_success_report():
    with pytest.raises(ValidationError):
        ReviewBundle.model_validate({"status": "verified", "report": "all passed"})


@pytest.mark.parametrize(
    "name,model",
    [
        ("review-bundle.v1.schema.json", ReviewBundle),
        ("verification-journal.v1.schema.json", VerificationJournal),
    ],
)
def test_persistent_schema_matches_typed_contract(name, model):
    assert json.loads((Path("contracts") / name).read_text()) == model.model_json_schema()
