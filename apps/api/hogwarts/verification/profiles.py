"""Operator-approved fixed harness protocol; no workload report is authoritative."""

from dataclasses import dataclass

from hogwarts.qa.contracts import deny
from hogwarts.qa.profiles import QAProfile


@dataclass(frozen=True)
class ReviewQAProfile(QAProfile):
    command: str = "acceptance"
    expected_tests: int = 2
    harness: str = "prototype-fixture.v1"

    def __post_init__(self):
        if (
            self.command != "acceptance"
            or self.expected_tests != 2
            or self.harness != "prototype-fixture.v1"
        ):
            deny("unapproved_review_harness", 403)
        QAProfile(
            self.name,
            self.image,
            "unittest",
            self.test_root,
            self.cpus,
            self.memory_mb,
            self.pids,
            self.timeout_seconds,
            self.lifetime_seconds,
        )

    @property
    def argv(self):
        return (
            "/usr/local/bin/python",
            "-I",
            "-B",
            "/opt/hogwarts/review-tests.py",
            self.test_root,
        )


def approved_review_profile(profile):
    if type(profile) is not ReviewQAProfile:
        deny("positive_test_execution_evidence_required", 403)
    profile.__post_init__()
    return profile
