import re
from dataclasses import dataclass
from typing import Protocol

from hogwarts.auth import Principal
from hogwarts.service import DomainError


class SandboxError(DomainError):
    pass


def deny(code, status=409):
    raise SandboxError(status, code)


COMMANDS = frozenset({"verify", "timeout", "memory", "pids", "disk", "cpu", "fail"})


@dataclass(frozen=True)
class RuntimeProfile:
    """Trusted operator code only; callers select a registered name, never construct one."""

    name: str
    image: str
    command: str = "verify"
    cpus: float = 0.5
    memory_mb: int = 128
    pids: int = 24
    timeout_seconds: int = 8
    lifetime_seconds: int = 45

    def __post_init__(self):
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", self.name)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", self.image)
            or self.command not in COMMANDS
            or not 0.1 <= self.cpus <= 2
            or not 64 <= self.memory_mb <= 512
            or not 8 <= self.pids <= 64
            or not 1 <= self.timeout_seconds <= 15
            or not self.timeout_seconds <= self.lifetime_seconds <= 60
        ):
            deny("invalid_runtime_profile", 403)


@dataclass(frozen=True)
class SandboxInfo:
    id: str
    run_id: str
    workspace_id: str
    state: str
    error_code: str | None
    result: dict | None


class SandboxController(Protocol):
    def create(
        self, actor: Principal, run_id: str, workspace_id: str, lease_token: str, profile: str
    ) -> SandboxInfo: ...
    def start(self, actor: Principal, run_id: str, lease_token: str) -> SandboxInfo: ...
    def execute(
        self, actor: Principal, run_id: str, lease_token: str, invocation_id: str
    ) -> SandboxInfo: ...
    def stop(
        self, actor: Principal, run_id: str, lease_token: str | None = None
    ) -> SandboxInfo: ...
    def destroy(
        self, actor: Principal, run_id: str, lease_token: str | None = None
    ) -> SandboxInfo: ...
