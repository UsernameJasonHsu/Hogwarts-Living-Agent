"""Profiles are approved operator configuration, never Agent-supplied commands."""

import re
from dataclasses import asdict, dataclass
from types import MappingProxyType

from hogwarts.qa.contracts import deny
from hogwarts.service import digest
from hogwarts.workspaces.git import relative_path


def safe_source_path(path):
    relative_path(path)
    for part in path.casefold().split("/"):
        if (
            part
            in {
                ".ssh",
                ".aws",
                ".azure",
                ".docker",
                "secrets",
                "id_rsa",
                "id_ed25519",
                "credentials",
                "credentials.json",
            }
            or part.startswith(".env")
            or part.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            deny("qa_sensitive_path", 403)
    return path


@dataclass(frozen=True)
class QAProfile:
    name: str
    image: str
    command: str = "unittest"
    test_root: str = "src"
    cpus: float = 0.5
    memory_mb: int = 128
    pids: int = 24
    timeout_seconds: int = 8
    lifetime_seconds: int = 60

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", self.name)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image)
            or self.command not in {"unittest", "timeout", "oom", "pids", "isolation"}
            or not 0.1 <= self.cpus <= 2
            or not 64 <= self.memory_mb <= 512
            or not 8 <= self.pids <= 64
            or not 1 <= self.timeout_seconds <= 15
            or not self.timeout_seconds <= self.lifetime_seconds <= 60
        ):
            deny("invalid_qa_profile", 403)
        safe_source_path(self.test_root)

    @property
    def fingerprint(self):
        return digest(asdict(self))

    @property
    def argv(self):
        if self.command == "unittest":
            return (
                "/usr/local/bin/python",
                "-I",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "/workspace/" + self.test_root,
                "-p",
                "test_*.py",
                "-v",
            )
        return (
            "/usr/local/bin/python",
            "-I",
            "-B",
            "/opt/hogwarts/qa-supervisor.py",
            "--probe",
            self.command,
        )


class QAProfileRegistry:
    def __init__(self, profiles):
        self.profiles = MappingProxyType({p.name: p for p in profiles})
        if len(self.profiles) != len(profiles):
            deny("duplicate_qa_profile", 403)

    def get(self, name):
        profile = self.profiles.get(name)
        if not profile:
            deny("unapproved_qa_profile", 403)
        return profile
