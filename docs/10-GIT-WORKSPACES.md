# M2.1 Git Workspace Manager

Internal local service only. It does not run project code, provide a sandbox,
call Codex, push, merge or deploy. The M1 API keeps its existing fake adapters.

## Data and integration

Reuse Principal, Project, Proposal, PrototypeRun, active lease and WorkflowEvent.
Add only GitWorkspace lifecycle/provenance in migration 0003. M1 has no Artifact
table; M2.1 returns WorkspaceDiff bytes/checksum/changed paths without introducing
object storage or replacing fake run evidence.

`workspaces/contracts.py` defines WorkspaceManager, RepositoryGrant, WorkspaceInfo
and WorkspaceDiff. `GitWorkspaceManager` implements it using LocalGit and the
existing SQLAlchemy session factory. After `alembic upgrade head` and a normal M1
create/start/claim flow, a trusted application caller can use:

```python
from pathlib import Path
from hogwarts.workspaces.contracts import RepositoryGrant
from hogwarts.workspaces.manager import GitWorkspaceManager

manager = GitWorkspaceManager(
    session_factory, private_workspace_root,
    (RepositoryGrant(project_id, Path(local_repository), expected_repo_ref, ("src", "tests")),),
)
info = manager.create(authenticated_worker, claimed_run_id, lease_token)
bundle = manager.export_diff(authenticated_worker, claimed_run_id, lease_token)
manager.cleanup(authenticated_worker, claimed_run_id, lease_token)
```

Principal must come from trusted authentication; grants must be operator config,
never reconstructed from request/agent JSON. The full SHA and requested paths come
from the claimed proposal. Validate repository identity and the intersection of
operator/project/proposal scope. No new HTTP endpoints are added. A worktree must
never be substituted for SandboxManager.

## Lifecycle

CREATING intent commits before Git. Unique run ID and SQL write locks serialize
operations across manager instances. READY retries return the same path; changed
configuration conflicts. CLEANED is a tombstone, not an implicit new attempt.
Separate runs have distinct UUID paths. FAILED/CLEANUP_FAILED preserve stable
errors; unsafe or unknown filesystem data is never blindly deleted. Cleanup can
be retried after operator correction. The original worker can clean up after lease
expiry; the owner human can clean up after cancellation. CREATING recovery adopts
only the exact registered, pinned clean worktree. Other partial states may need
operator intervention. No background reconciler is included.

Events reuse WorkflowEvent with source=git_workspace and aggregate_id=run_id.
The existing proposal-event API is unchanged; inspect the internal event store for
workspace events. Audit payloads never contain lease tokens, patch contents or raw
Git stderr. Checksummed diff output is not a claim that project tests passed.

## Safety limits

- Only registered ordinary local, non-bare SHA-1 repositories; detached worktrees.
  Source branch/index/files remain unchanged. Source Git administrative metadata
  and objects necessarily change. No fetch/push/merge/project commands exist.
- Shell-free argv, fixed executable, timeout, minimal child environment, disabled
  hooks/fsmonitor/protocols and empty global/system config. Reject config includes,
  filters, alternate stores and unsupported extensions.
- Validate the commit tree before checkout: reject links/gitlinks, unsafe names,
  ambiguous case paths, `.gitattributes`, `.gitmodules` and `.lfsconfig`. These are
  explicit compatibility restrictions, not silently supported features.
- Validate actual symlinks/reparse points/hard links, gitdir/backlink and pinned
  detached HEAD before export/removal. Export staged/unstaged/new/deleted/binary
  and ignored files using a private index. Deny unauthorized paths before returning
  any patch; reject snapshot changes during export.
- Export safeguards: 8 MiB per file, 64 MiB snapshot, 16 MiB Git stdout. These do
  not constitute CPU/disk/process/network sandbox isolation.
- Workspace roots and Git metadata must be private service-owned directories,
  protected from hostile concurrent same-user code. This is not an OS security
  boundary. Do not run arbitrary third-party workloads here. M2.2–M2.4 remain out
  of scope, including real execution, isolation and artifact publishing.

## Verification

With Git, Python 3.12 and a dedicated TEST_DATABASE_URL:

```powershell
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
python -m pytest -v --junitxml=test-results/junit.xml
```

Tests use disposable repositories under checked test roots, never the real project
as a workload. Existing M1 regressions remain; fixture cleanup only adds handling
for Windows read-only Git objects. Migration roundtrip/check includes 0003. Native
link creation may be unavailable on Windows: report skips separately from Linux
evidence. An unpushed M2.1 branch has no remote CI result.
