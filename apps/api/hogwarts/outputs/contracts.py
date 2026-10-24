from dataclasses import dataclass
from typing import Protocol

from hogwarts.auth import Principal
from hogwarts.service import DomainError

MAX_ARCHIVE = 12 * 1024 * 1024
MAX_FILE = 4 * 1024 * 1024
MAX_TOTAL = 8 * 1024 * 1024
MAX_FILES = 256
MAX_ENTRIES = 512


class OutputError(DomainError):
    pass


def deny(code, status=409):
    raise OutputError(status, code)


@dataclass(frozen=True)
class OutputGrant:
    """Trusted operator mapping; never constructed from an Agent request."""

    profile_name: str
    target_prefix: str


@dataclass(frozen=True)
class OutputFile:
    source_path: str
    target_path: str
    content: bytes
    sha256: str


@dataclass(frozen=True)
class ImportInfo:
    id: str
    run_id: str
    workspace_id: str
    sandbox_id: str
    state: str
    archive_sha256: str | None
    manifest_artifact_id: str | None
    manifest_sha256: str | None
    file_count: int
    assessment: str = "unverified"


class SecureOutputImporter(Protocol):
    def import_output(
        self,
        controller: object,
        actor: Principal,
        run_id: str,
        workspace_id: str,
        sandbox_id: str,
        lease_token: str,
    ) -> ImportInfo: ...
