# ADR 0010 — M2.3.5 End-to-End Verification and Human Review

Status: architecture and test plan BEFORE implementation, 2026-10-24.

## Architecture / boundaries

Add an internal PrototypeVerificationOrchestrator, not a new public HTTP endpoint.
Reuse M1 authenticated Principal, claimed Run and original lease, command/CAS,
ApprovalRequest, budget release, WorkflowEvent and MergeRequest intent. Reuse the
existing workspace, output sandbox/controller, importer, patch builder, QA runner,
QA input reconstruction and immutable ArtifactStore. No real Codex or reconciler.

verify(worker, run_id, lease, runtime_profile, qa_profile) accepts identifiers only.
Operator construction supplies existing services and an approved ReviewQAProfile.
One durable verification journal per Run commits execution intent before effects;
concurrent callers get busy/unknown, never a second pipeline execution. Crash without
a complete authenticated result capsule is cleanup/manual investigation only.
Complete capsules allow publication-only recovery. Profiles/context changes conflict.

Pipeline: Workspace create -> output Sandbox create/start/execute -> stopped-output
Import -> immutable Patch/BuildProof -> new QA sandbox -> acceptance gate -> immutable
ReviewBundle -> transactional presentation. Existing modules retain their checks.
Revalidate current grants/lease and reconstruct QA inputs before acceptance; bind
manifest/archive/files, patch/proof, snapshot, QA journal/HMAC result/attestation/logs,
actual profile/image/argv/limits/outcome/exit, and hashes to the same Run.

## Zero tests / evidence sufficiency

Legacy unittest exit 0 is insufficient and is blocked. Never parse stdout/JUnit
supplied by a workload as acceptance authority. Add a separate pinned review-QA
image and ReviewQAProfile with an image-owned fixed acceptance harness. This MVP
checks the deterministic producer fixture (prototype.txt), not arbitrary projects.
The image owns two independent unittest cases, never imports workload Python, and
returns zero ONLY after exactly two tests ran with no failures/errors/skips. Zero
tests or malformed harness execution returns nonzero. Controller acceptance binds
the approved image, exact command and this reviewed exit protocol; all logs remain
unverified. General project QA requires another operator-reviewed harness protocol.
This is an explicit compatibility limit, not a semantic correctness/coverage claim.
Existing M2.3.4 profiles, drivers, schemas and tests are unchanged.

## Review state and governance

Gate verdicts: READY_FOR_HUMAN_REVIEW, FAILED, BLOCKED, UNKNOWN. READY requires a
nonempty applicable patch, authenticated passing QA, known positive test execution,
complete cleanup, and all required immutable evidence. No Artifact is promoted.
Known QA failure stays FAILED; denial/revocation or insufficient evidence BLOCKED;
ambiguous execution UNKNOWN. Unauthorized reads never become a new capability.

On READY, use M1 CAS transitions through REVIEWING and set Run status explicitly
READY_FOR_HUMAN_REVIEW. Keep Proposal REVIEWING until the new bundle-bound human
decision transaction: the legacy M1 human endpoint cannot bypass bundle validation.
Create a pending ApprovalRequest bound to bundle SHA, proposal revision, Base,
target and expiry. Human review re-reads every artifact via existing owner access,
rechecks current grants/profile/journals, expected version, bundle hash and expiry.
Then atomically use M1 CAS/decision to create an internal MergeRequest only.
No Git merge executor exists here. Cancellation invalidates review. Approval and
intent are idempotent through existing M1 command, with no automatic SCM side effects.

## Persistence / schema / planned files

Reuse IdempotencyRecord and ApprovalRequest; no SQL DDL, Alembic head remains 0006.
Add versioned review-bundle.v1 and verification-journal.v1 JSON Schemas; strict
frozen Pydantic models define the persistent contract. WorkflowEvent source
prototype_verification records stages/verdicts/denials without tokens or seal keys.
DB outage can also prevent Audit: return unavailable, never claim it was persisted.

Add hogwarts/verification/{contracts,profiles,gate,orchestrator}.py,
infra/review-qa/{Dockerfile,supervisor.py,tests.py}, tests/test_verification*.py,
docs/21-PROTOTYPE-VERIFICATION.md, schemas, this ADR and README append.
Preserve all existing implementations/tests. Initial HEAD 50d6d77 and 121 hashes
are saved outside the repo. Actual initial worktree was clean, contrary to prompt
baseline; work proceeds on m2-3-5-end-to-end-verification without commits.

## Test plan (before implementation)

1. Strict protocols/schemas; invalid profile/image/extra host paths; legacy zero-test
   profile blocked. Trusted fixed harness success/failure/zero test guard in Linux.
2. SQLite/PostgreSQL with real Git/store/import/patch, explicit fake execution for
   portable gate tests: pass/fail/timeout/unknown, bindings, tampered/missing evidence,
   BuildProof/patch/QA mismatch, grant revocation, wrong Principal/Run/lease, expiry.
3. Concurrent service instances: one execution; immutable bundle/idempotent retry;
   pending/unknown crash cleanup, capsule publication recovery, DB/storage failures.
4. Owner human accept/reject/defer, stale revision/bundle/expiry, cancellation,
   cross-owner/worker denial; exactly one intent; no Git effects or verified promotion.
5. REAL Linux output Sandbox -> Import -> Patch -> fresh QA -> review for scenarios
   A success, B real QA failure, C evidence tampering, D revoked grants, E restart.
   Pin runtime/reader/review-QA images. Persist success/failure bundles and raw logs.
6. Complete original 831-case regression plus new cases on Linux and Windows;
   Ruff check/format, SQLite/PostgreSQL migration roundtrip/drift; raw JUnit/logs,
   command/image/source fingerprints. Skips are not passes. Remote CI not claimed.

Limits: trusted controller/DB/private roots/operator-approved images, no malicious
host/admin or kernel-escape defense, no background cleanup/retry, no production
deployment, arbitrary-project harness, independent code reviewer or real Codex.
Acceptance pending actual implementation and execution.
