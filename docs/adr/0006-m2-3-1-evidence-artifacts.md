# ADR 0006 — M2.3.1 Evidence Schema & Local Artifact Store

Status: accepted implementation design, 2026-10-24.

## Scope and existing foundations

Preserve the dirty `m2-1-git-workspace-manager` branch and all M1/M2.1/M2.2 behavior.
M1 ArtifactStore is a fake inline protocol; Run.evidence is fake workflow evidence.
M2.2 SandboxInstance.result is controller execution JSON, not a published artifact.
Add a separate internal ArtifactStore protocol; do not replace fake_adapters, mutate
Run.evidence, add HTTP routes, collect sandbox files, implement QA, Codex or M2.4.

## Versioned evidence and trust

Ship Pydantic frozen/extra-forbidden models and a generated JSON Schema v1.
The immutable metadata contains server UUID, SHA-256/size/media/kind, Run/Workspace/
Sandbox IDs, producer identity, server creation time, pinned commit/input hash and
a snapshot of controller state/execution timestamps/status derived from existing
WorkflowEvents and SandboxInstance. Caller content is bytes, never a host pathname.

An EvidenceDocument is a versioned view combining immutable hashed metadata with
storage lifecycle and assessment. Define verified / unverified / failed explicitly:
verified requires a future trusted verifier; this milestone has NO verified issuer
or promotion endpoint. All uploads are agent submissions and remain unverified,
including JSON that claims success. failed assessment means storage/integrity
failure, not proof of code failure. Controller execution status is a separate field.
SHA-256 match means integrity only. No QA/test verdict is inferred from content,
human upload, sandbox exit 0 or a matching checksum. Agent cannot set source/status,
timestamps, SHA, IDs, verifier or storage path through upload metadata.

## Authorization and immutable identity

Reuse GitWorkspaceManager's existing authenticated worker/owner/run/lease checks.
Create requires the active assigned worker and matching READY Workspace + Sandbox
with the original lease hash. Reads require explicit run + artifact IDs: active
assigned worker with matching lease, or owner human for historical results, including
after workspace/sandbox cleanup. No cross-run/project/owner access. No new principals
or permission grants. Principal must originate from trusted M1 authentication.

Server-generated UUID identifies an artifact independently of content; equal bytes
in different runs never grant access to each other. Unique (run, producer, hashed
idempotency key) serializes retries. Changed bytes/kind/media/bindings conflict.
Same completed retry revalidates immutable metadata and bytes before returning.
No overwrite/update/delete API. Different keys may create distinct immutable IDs.

## DB/storage protocol and failure

Migration 0005 adds artifact_metadata: FKs, owner/producer/lease hash, unique retry
key, request hash, immutable JSON metadata + metadata SHA, content SHA/size, lifecycle
PENDING/READY/FAILED/CORRUPT, version and stable error code. Persist PENDING + Audit
before filesystem effects. Lock the artifact row with an SQL write before storage.
Write with exclusive create, fsync, read-only file mode; READY + Audit commit after
durable complete bytes. SQLite and PostgreSQL use independent session/row locks.

DB/filesystem are not atomic. A crash/commit failure can leave a PENDING row and
complete file: the exact same request may adopt it only after full integrity checks.
Partial/mismatching file is never overwritten; mark FAILED/CORRUPT and retain for
operator inspection. Failed writes are terminal for that key; a new key can create
a new artifact. No automatic garbage collector or reconciler. DB unavailable means
audit persistence may also be unavailable: return a stable 503, never claim audit
was saved. Inject these failures in tests and report the boundary.

## Filesystem boundary

Operator supplies a private absolute store root outside source repositories and
workspaces, never mounted into workloads. Only server UUID filenames in objects/;
no caller directories, names, URIs or path-based import. Validate every ancestor,
reject symlinks/reparse points/special files/hardlinks. On Linux require service
ownership/private directories; use O_NOFOLLOW, exclusive creation, lstat/fstat inode
comparison and single-link validation. Read bounded bytes from the checked fd and
verify length/SHA and canonical immutable metadata hash before returning anything.
Missing or tampered storage fails closed and is audited; no partial content returned.

Windows gets ordinary developer filesystem checks but not a claim of race-proof ACL
isolation. Trusted service-owned root and DB are prerequisites on both platforms;
hostile processes with the same OS credentials/admin access are outside the boundary.
No generic secret scanner or encrypted/WORM filesystem is introduced. Enforce 16 MiB
per artifact; total retention quota and backup/restore are future operator concerns.

## Planned files

apps/api/hogwarts/artifacts/{contracts,filesystem,store}.py; append ArtifactMetadata;
contracts/evidence.v1.schema.json; infra/migrations/versions/0005_m2_3_1_artifacts.py;
tests/test_artifacts.py; docs/14-EVIDENCE-ARTIFACTS.md; this ADR; verification report.
README gets only a scoped status/link update. Existing runtime/security/tests remain
byte-identical. Existing Linux CI discovers new tests; no remote CI pass claim.

## Tests defined before implementation

1. Schema v1 roundtrip/JSON-schema agreement, reject unknown version/extra fields,
   forged verified/source/timestamp/hash/path metadata; server-origin provenance.
2. Create/read/describe empty/text/binary bytes, SHA-256, immutable identity/content,
   storage permissions; checksum never promotes upload to verified.
3. Same-key retry, changed request conflicts, equal bytes across keys/runs isolated;
   concurrent same/different requests with separate Store instances/sessions.
4. Owner/worker/run/workspace/sandbox/lease denials, expiry, cancellation, historical
   owner reads after cleanup, missing artifact and cross-run reads.
5. Traversal/absolute/ADS/invalid UUID, unsafe roots, symlinks/hardlinks/reparse paths,
   special files, missing files, same-size and size-change tampering; no outside writes.
6. Metadata tampering; filesystem create/fsync/read/partial failure, DB failure before
   intent and after durable file, exact-request recovery, audit failure redaction.
7. SQLite/PostgreSQL migration up/down/up + drift; complete existing 295-case Linux
   regressions with real M2.2 Docker tests; Windows skips reported separately.

Only executed test evidence can establish completion. No commit/push/merge/deploy.


Final implementation/results: docs/14-EVIDENCE-ARTIFACTS.md and docs/15-M2-3-1-VERIFICATION.md. Verified remains reserved; no verified issuer exists in M2.3.1.
