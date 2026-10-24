from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from hogwarts.auth import Principal
from hogwarts.service import DomainError


class WorkspaceError(DomainError):
    """Stable public error code; never include raw Git stderr or host credentials."""


@dataclass(frozen=True)
class RepositoryGrant:
    """Trusted operator configuration, never reconstructed from request JSON."""

    project_id: str
    repository: Path
    repo_ref: str
    allowed_paths: tuple[str, ...]


@dataclass(frozen=True)
class WorkspaceInfo:
    id: str
    run_id: str
    path: Path
    base_commit: str
    state: str
    version: int
    error_code: str | None


@dataclass(frozen=True)
class WorkspaceDiff:
    workspace_id: str
    base_commit: str
    changed_files: tuple[str, ...]
    patch: bytes
    sha256: str


class WorkspaceManager(Protocol):
    def create(self, actor: Principal, run_id: str, lease_token: str) -> WorkspaceInfo: ...
    def export_diff(self, actor: Principal, run_id: str, lease_token: str) -> WorkspaceDiff: ...
    def cleanup(
        self, actor: Principal, run_id: str, lease_token: str | None = None
    ) -> WorkspaceInfo: ...
