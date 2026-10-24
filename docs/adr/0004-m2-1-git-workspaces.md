# ADR 0004: M2.1 Git Workspace Manager

Status: accepted design, 2026-10-24. Scope: workspace preparation/diff/cleanup only.

## Existing foundations and integration boundary

Reuse M1 Principal, Project/Proposal/PrototypeRun, active lease, DomainError and
WorkflowEvent. M1's ArtifactStore is a protocol with inline fake evidence, not a
durable artifact table. Add only GitWorkspace lifecycle metadata (migration 0003).
Diff export returns immutable bytes, checksum and changed paths; object storage,
runtime execution and sandbox integration are not part of M2.1. M1's default
fake_adapters() and engineering API remain unchanged.

## Interface and authorization

WorkspaceManager.create/export_diff take an authenticated Principal, run ID and
lease token. Paths, repository URL, SHA and project ID are not free-form command
arguments. Derive these from the claimed M1 run and proposal, then intersect with
an explicit operator-supplied RepositoryGrant mapping (project ID, local repository,
expected repo_ref and allowed paths). A grant is configuration supplied by a trusted
caller, not permission obtained from generated content. Only an active assigned
worker can create/export; its original lease or the owner human can clean up after
expiry/cancellation. No new external authorization/approval system is introduced.

## Git and filesystem safety

Use full lowercase 40-character SHA-1 values and verify local object type=commit.
Create detached linked worktrees under a private operator-owned root, separate
from source repositories. Source branches/index/files remain untouched; Git's
administrative worktree/object metadata necessarily changes. Never fetch, push,
merge, run project commands, install dependencies or initialize submodules.

Use argv with shell=False, an absolute Git executable, timeout, minimal environment,
empty global/system config and disabled hooks/fsmonitor/protocols. Inspect repository
metadata/config before Git; reject includes, external filters, alternate object
stores and unsupported repository extensions. Only ordinary local non-bare source
repositories are supported initially. Inspect the commit tree before checkout;
reject symlink/gitlink entries and nonportable/unsafe paths. Validate actual working
tree entries before export/removal, including reparse points and hard links. Check
the exact registered worktree gitdir/backlink and pinned detached HEAD before reads.

For this narrow initial implementation, `.gitattributes`, `.gitmodules` and `.lfsconfig`
are unsupported and rejected before checkout/export. This avoids attribute-driven
content conversion and submodule/LFS behavior; it is an explicit compatibility limit.

Workspace roots and Git metadata are trusted service-owned areas, not writable by
untrusted concurrent code. Worktree content is untrusted data. This is fail-closed
validation, NOT an OS security boundary against malicious same-user processes.
Container/MicroVM/network/CPU isolation belongs to later M2 subphases.

## Diff completeness

Use a private temporary Git index initialized from the pinned commit; force-stage
all regular files (including ignored files), without changing source/worktree indexes.
Export binary Git diff with external diff/textconv and rename detection disabled.
Validate changed paths against all grants before returning bytes. Reject symlinks,
unsafe metadata, oversized exports and concurrent content changes; never export a
partial unauthorized patch. The result checksum is persisted alongside lifecycle
metadata and an audit event; it is not a test-success claim or an integration approval.

## Lifecycle, idempotency and failure

Persist CREATING intent before Git operations. Unique run_id and a DB write lock
serialize concurrent operations across service instances on PostgreSQL and SQLite.
States: CREATING, READY, FAILED, CLEANING, CLEANUP_FAILED, CLEANED. Repeated READY
creation returns the same workspace after provenance validation; changed request
fingerprints conflict. CLEANED is a tombstone (no silent resurrection). Repeated
cleanup is a no-op. Re-entry after a process crash can adopt only a pinned, clean,
properly registered CREATING worktree; unknown/conflicting filesystem data is never
deleted. Cleanup failure remains retryable and audited; no automatic background
reconciler is added. Git and DB cannot be one atomic transaction: retain durable
intent and deterministic paths, and document the need to retry after commit failures.

Audit events reuse the M1 envelope with source=git_workspace and stable error codes.
Never persist Git stderr, commands containing credentials, lease tokens or patch
content in audit payloads. No branch merge or deployment authority is granted.

## Planned acceptance tests (defined before implementation)

1. Local full SHA checkout, detached HEAD, independent run paths, unchanged source.
2. Modify/add/delete/binary/staged/ignored files; exact changed paths, checksummed diff.
3. Deny missing grant, wrong owner/agent/lease, expired lease, wrong repo and path scope.
4. Deny branch/tag/noncommit/missing SHA; tree symlinks/submodules/unsafe names.
5. Deny working symlinks/hardlinks/metadata tampering; never touch outside sentinels.
6. Ignore hostile inherited global configuration; hooks/filters cannot execute.
7. Concurrent duplicate create/cleanup, parameter conflicts, failure audit and retry.
8. Service restart reuse and CREATING recovery; cleaned tombstone and cleanup after expiry.
9. SQLite/PostgreSQL migration up/down/check and full 116-case M1 regression suite.

References: https://git-scm.com/docs/git-worktree,
https://git-scm.com/docs/git-diff, https://git-scm.com/docs/git (environment variables).
