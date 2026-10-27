# M2.3.5 Prototype Verification and Human Review

Architecture and original preimplementation test plan: [ADR 0010](adr/0010-m2-3-5-prototype-verification.md).

## Operator construction

Construct existing GitWorkspaceManager, OutputController / OutputDockerDriver,
LocalArtifactStore, LocalOutputImporter, LocalGitPatchBuilder and
LocalTrustedQARunner with their separate private roots and approved pinned images.
Register `ReviewQAProfile("acceptance", exact_review_image_id)` in the existing
QAProfileRegistry. Construct `LocalPrototypeVerificationOrchestrator(qa, private_root)`.

```python
info = orchestrator.verify(worker, run_id, original_lease, "capture", "acceptance")
# An authenticated owner human supplies the exact immutable bundle and revision:
result = orchestrator.review(
    human, run_id, info.verification_id, info.bundle_sha256,
    info.proposal_version, "ACCEPTED", human_idempotency_key,
)
assert result["merged"] is False
orchestrator.cleanup(human, run_id)
```

Worker/owner identities originate in M1 authentication. Operator construction is
trusted code, not data from an Agent. No host path, shell, arbitrary Docker flags,
image override or archive bytes are accepted by verify/review calls.

## Acceptance gate and trust

Pipeline uses actual existing workspace/sandbox/import/patch/QA methods and their
authorization. Reconstruct QA inputs again; verify original import, SHA-256 and
provenance, patch/proof, result tree and snapshot, authenticated QA journal/capsule,
attestation, actual argv/profile/image/limits/exit, logs and cleanup. Nonempty
applicable patch and positive trusted test execution are necessary for READY.
All required references are checked again within final publication/review DB
transactions using ArtifactStore's existing authorization and safe FD reads.

ReviewBundle v1 includes explicit Base/result tree/input hash/changed paths,
Patch/BuildProof/QA IDs, profile/image and immutable artifact references. SHA-256
proves integrity only. Bundle, Patch and QA artifacts remain **unverified**.

The new pinned `infra/review-qa` image contains two immutable fixture acceptance
tests. Its supervisor calls only the image-owned harness, which NEVER imports or
executes producer code. The harness validates deterministic `prototype.txt` and
regular bounded UTF-8 content; success exit 0 requires exactly two tests with no
failures/errors/skips. Zero tests exit 75; ordinary test failures exit 1. Profile
identity and exact image ID bind this exit protocol. Logs are retained as data,
never parsed to grant authority. Legacy unittest profiles (including exit 0 after
zero discovery) are insufficient and are rejected before execution.

This MVP verifies the existing deterministic producer fixture. Arbitrary project
test suites require another reviewed image-owned harness and acceptance policy.
It does not claim independent source review, test adequacy or semantic correctness.
The controller, approved image, Docker daemon, DB and private roots are trusted;
hostile administrator/same-user interference and kernel escapes are not covered.

## States, retry and human decisions

Verification journal: PENDING -> EXECUTING -> PUBLISHING -> COMPLETE. Captured
results are authenticated by existing CapsuleVault; journal CAPTURED is reserved
for schema compatibility but capture may be adopted directly from EXECUTING.
Known authorization/evidence denial retains BLOCKED; ambiguous interruption UNKNOWN.
A concurrent or invalid request cannot revoke another caller's active execution.
No completed capsule means no automatic replay. Complete capsules permit only
artifact publication/finalization retry, never a second Sandbox/QA execution.

COMPLETE means a bundle was published, not QA passed. Verdict remains
READY_FOR_HUMAN_REVIEW / FAILED / BLOCKED / UNKNOWN. Run.evidence references the
immutable bundle with `verified: false`. Ready/final failure releases the M1 budget
exactly once. A manual cleanup of interrupted BLOCKED/UNKNOWN also terminalizes
the claimed Run and releases its reservation. No background reconciler exists.

Ready Run status is explicit; Proposal remains REVIEWING. A one-hour pending
ApprovalRequest binds bundle hash, revision, Base and target. Owner human review
rechecks artifacts, HMAC result, grants/profile, cancellation/version and expiry;
then atomically uses existing M1 CAS/decision. Accept creates only an existing
MergeRequest intent referencing the bundle-bound human ApprovalRequest. Reject or
defer creates no intent. Legacy M1 acceptance cannot skip the bundle gate.

Terminal worker verify retry returns previously published identifiers only after
original worker/lease and current configuration checks. It does not grant historical
artifact content access; owner human review uses existing historical Store access.
Historical immutable bundle status is not rewritten by later revocation: review
denials and cancellation are separately audited and prevent acceptance.

Manual cleanup authorizes original worker/lease or owner human and invokes existing
QA, output Sandbox and workspace cleanup. Failed cleanup remains an operator issue;
the runtime has no automatic retry loop or production deployment side effects.

## Schema / migration / evidence

New JSON contracts: review-bundle.v1 and verification-journal.v1. Reuse M1
IdempotencyRecord/WorkflowEvent/ApprovalRequest/MergeRequest; no SQL migration.
Alembic head remains 0006 and must retain SQLite/PostgreSQL drift/roundtrip tests.
Audit source prototype_verification records lifecycle, review denials and decisions;
no tokens/seal keys/raw content. DB/Audit outage is unavailable, never a saved audit.

Tests distinguish explicit fake execution in portable cross-module contracts from
real Linux producer -> import -> patch -> new QA -> human review scenarios. Windows
is development host only. Actual final counts/JUnit/logs and acceptance limitations
belong to the separately generated verification report, not inferred from this guide.
No real Codex, M2.4, commit/push/merge or deployment is performed by this component.
