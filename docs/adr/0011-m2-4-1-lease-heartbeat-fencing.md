# M2.4.1 Lease Heartbeat & Fencing — architecture and preimplementation test plan

Status: DESIGN BEFORE SOURCE CHANGES; 2026-10-24. Acceptance pending actual tests.

## Existing state and scope
HEAD 50d6d77 on m2-3-5-end-to-end-verification; 136 source fingerprints and dirty status saved externally. Preserve the uncommitted M2.3.5 implementation. Reuse M1 Principal, ownership, Proposal/Run, Atomic Claim, IdempotencyRecord and WorkflowEvent. Existing module authorization converges on GitWorkspaceManager._authorize; artifact, import, patch, QA and verification recheck it before final publication. Existing checks alone do not serialize revocation with result commits.

## Architecture
Add an internal DurableLeaseManager (trusted controller API, not a new unauthenticated HTTP route). Claim issues an opaque random credential plus monotonic generation and lease version. Credential is never reused or rotated by heartbeat; it identifies exactly one claimed Run generation. Heartbeat requires credential, expected generation/version and a bounded idempotency key; atomic DB CAS extends a still-active lease by an operator-configured bounded TTL. No resurrection after expiry, no automatic reassignment/retry, no unknown-execution replay. A new worker requires a separate authorized Run attempt; takeover/reconciler is outside scope.

Migration 0007 adds generation, version, heartbeat timestamp, revocation timestamp/reason and stop-hook state to Run. Existing claimed records migrate to generation/version 1; unclaimed records stay 0. Revocation or explicit expiry increments generation and version, retains the original credential solely for safe cleanup, and persists a pending stop intent. Human cancellation uses the same transactional invalidation. No capability is granted by generated content.

Active authorization acquires a DB write fence on Run and registers the observed generation/credential for a commit-time check. The same transaction holds the fence through state/artifact publication. Commit rechecks wall-clock expiry and generation, rejecting late results even if a long operation crossed its deadline. This closes revocation-versus-commit races without replacing existing content/hash/grant checks. A revocation ordered after an already committed result cannot retroactively erase immutable evidence. The original worker or owner human can still stop/destroy after expiry/revocation; this grants no execution/publication rights.

Human review remains allowed after ordinary lease expiry of completed immutable evidence, but explicit revocation/cancellation blocks acceptance, including cached review requests. Review takes a generation fence through Merge Intent commit. Historical artifact reads remain allowed to the owner; no evidence is promoted to verified.

Stop hooks run outside the revocation transaction through trusted controller callbacks (no agent commands). Persist PENDING before calling and CONFIRMED only after success; failure leaves retryable FAILED and emits a stable audit code. Retry may repeat only idempotent stop, never execution. DB outage means no stop-completion/audit success claim. No background scanner, Resource Reconciler or GC.

## Important limits
Existing sandbox controller transactions can hold DB locks during bounded Docker operations. Heartbeat/revocation can wait behind that lock; commit-time wall-clock checks reject late success. Existing absolute sandbox deadlines are never extended by heartbeat. Timely preemption is best effort via safe stop; this is not a distributed resource reconciler or a promise of immediate process termination. SQLite is development-only; both DB backends must be tested. Trusted DB/controller/private roots remain prerequisites.

## Planned changed files
Add leases/{contracts,manager,fencing}.py and package init, migration 0007, tests/test_leases.py and Linux integration tests, docs/22-LEASE-HEARTBEAT-FENCING.md and ADR 0011. Scoped edits: models.py, service.py (claim/cancel/fake execution), workspaces/manager.py (shared active fence), verification/orchestrator.py (human review fence), README append. Existing tests stay intact; migration drift/roundtrip must pass.

## Test contract (before implementation)
1. Heartbeat advances version/expiry, exact retry is stable, changed key payload conflicts; concurrent requests with the same expected version have one winner; stale generation/version, expired/revoked/cancelled/wrong owner/worker/credential fail closed.
2. Generation increases on invalidation; repeated revocation is idempotent; restart uses DB state, expired leases never revive; DB failures do not manufacture a heartbeat or audit.
3. Commit-time expiry rolls back late writes. Two DB sessions demonstrate revoke/publication serialization; artifact publication after revoke/expiry is denied and immutable committed objects remain historical unverified evidence.
4. QA revocation during execution rejects late result/attestation publication without replay. Verification revocation blocks READY/bundle finalization. Human revocation blocks late review/Merge Intent, while ordinary completed lease expiry retains historical review semantics.
5. Human cancellation revokes atomically; safe cleanup remains available; stop success/failure/retry are durable and audited, no arbitrary shell/runtime parameters.
6. Real Linux deterministic sandbox/QA, new fencing scenarios and complete existing 941 test IDs. Separate Windows skips and exact full-run counts. SQLite/PostgreSQL migration up/down/drift, Ruff check/format, JUnit/raw logs/source fingerprints. Remote CI and unexecuted checks remain unverified.


## Operation notes
Construct DurableLeaseManager(existing_session_factory, ttl_seconds=300) in trusted controller code. Use Heartbeat(lease_token=original_credential, generation=claim_generation, expected_version=last_version); keep a stable key for an uncertain heartbeat retry. Responses are historical snapshots, not capabilities. No automatic heartbeat thread is introduced; the trusted worker/controller schedules explicit calls.

The opaque credential is permanently bound to lease_issued_generation. Active and human review fences require generation == issued_generation > 0; revocation advances generation without changing issued_generation. Heartbeat never rotates that binding. Existing resource lease hashes identify the original issuance. No in-place takeover/reuse exists.

Owner-human revoke(run, expected_generation, key) commits invalidation before stop(actor, run, trusted_idempotent_callback). A callback can call existing Controller.stop and QA.cleanup; it must never execute work. Stop may repeat after a crash, so callbacks must be idempotent. CONFIRMED cannot be downgraded by a competing failed stop. Pending/failed stop is visible in durable Run state. Human cancellation schedules the same pending intent; callers explicitly drain it. No scanner or automatic resource deletion is added.

Heartbeat, publication and revocation use trusted UTC wall clocks; synchronized service clocks and the trusted DB are assumptions. Transaction fences serialize commits, not arbitrary external Docker effects. Workload absolute deadlines remain independent. A new generation does not authorize a new worker on this Run. Migration downgrade removes fencing history and is tested only on disposable databases.


## Regression-driven transaction amendment
The first full regression exposed premature flush and a PostgreSQL lock-order deadlock. Preserve existing before_commit failure-injection semantics: fence queries use no_autoflush, before_commit checks do not flush, and after_flush_postexec checks validate the actual flushed state before commit. This retains fail-closed expiry without hiding pending journals from existing observers.

All operations that acquire both Run and resource/journal locks acquire Run first. Scoped reorderings in the existing Patch Builder and QA Runner are required integration changes; cleanup/historical authorization obtains a non-authorizing Run lock as well. M1 claim/cancel acquire Run before Proposal CAS. Verification state transactions use the same order. No existing regression test is edited. Actual full-run failures and subsequent rechecks are retained in external evidence; architecture acceptance remains pending final tests.
