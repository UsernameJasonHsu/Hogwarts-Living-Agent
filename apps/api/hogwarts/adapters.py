"""Pure fake adapters. Never run commands, touch a repository or make network calls."""

from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol


@dataclass(frozen=True)
class Sandbox:
    id: str
    base_commit: str


class CodingRuntime(Protocol):
    def execute(self, sandbox: Sandbox, task: dict, run_id: str) -> bytes: ...


class SandboxManager(Protocol):
    def create(self, repository: str, commit: str, run_id: str) -> Sandbox: ...
    def destroy(self, sandbox_id: str) -> None: ...


class ArtifactStore(Protocol):
    def put(self, content: bytes) -> dict: ...


class TestRunner(Protocol):
    def run(self, sandbox: Sandbox) -> dict: ...


class FakeSandboxManager:
    def create(self, repository: str, commit: str, run_id: str) -> Sandbox:
        return Sandbox("fake:" + run_id, commit)

    def destroy(self, sandbox_id: str) -> None:
        pass


class FakeCodingRuntime:
    def execute(self, sandbox: Sandbox, task: dict, run_id: str) -> bytes:
        return b"M1 FAKE PATCH FIXTURE: no repository was read or modified.\n"


class FakeArtifactStore:
    def put(self, content: bytes) -> dict:
        return {
            "scheme": "inline-fake",
            "sha256": sha256(content).hexdigest(),
            "size_bytes": len(content),
            "content": content.decode(),
            "verified": False,
        }


class FakeTestRunner:
    def run(self, sandbox: Sandbox) -> dict:
        return {
            "adapter": "FakeTestRunner",
            "fixture_status": "PASS",
            "verified": False,
            "command": None,
            "exit_code": None,
            "message": "No project tests executed",
        }


@dataclass
class Adapters:
    runtime: CodingRuntime
    sandbox: SandboxManager
    artifacts: ArtifactStore
    tests: TestRunner


def fake_adapters():
    return Adapters(
        FakeCodingRuntime(), FakeSandboxManager(), FakeArtifactStore(), FakeTestRunner()
    )
