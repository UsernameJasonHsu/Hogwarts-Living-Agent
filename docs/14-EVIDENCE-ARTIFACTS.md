# M2.3.1 Evidence Schema & Local Artifact Store

## Scope and trust

Internal typed storage service, not an HTTP API, collector, verifier, QA runner or
Codex integration. M1 fake_adapters()/Run.evidence and all M2.1/M2.2 code are unchanged.
Uploaded bytes are opaque agent submissions. No file path, shell command, source,
timestamp, checksum or verified flag can be supplied through ArtifactUpload.
The local root and DB belong to the trusted service and are never workload mounts.

The ArtifactStore Protocol is separate from the M1 fake protocol because M1 has
neither durable artifact identity nor run-scoped authorization. Do not substitute
the real store into the fake workflow without a separately reviewed integration.

## Evidence v1

`contracts/evidence.v1.schema.json` is generated from frozen, extra-forbidden
Pydantic models in artifacts/contracts.py. Unsupported schema versions fail closed.
EvidenceDocument has:

| Field | Meaning |
|---|---|
| schema_version | Wire format version 1 |
| artifact | Immutable identity, SHA/size/type, provenance and controller snapshot |
| storage_state | PENDING / READY / FAILED / CORRUPT |
| assessment | verified / unverified / failed, with an explicit reason |
| integrity | not_checked / valid / failed for this operation |

Immutable provenance binds Artifact/Run/Workspace/Sandbox IDs, authenticated producer,
server creation time, pinned base commit and sandbox input SHA. ExecutionSnapshot
uses SandboxInstance state/version/result and matching existing WorkflowEvents for
started_at/finished_at; absent facts remain unknown/null. Its status is an execution
metadata snapshot (unknown/succeeded/failed), not a verdict about uploaded content.
It remains the original snapshot on retries even if the sandbox later changes state.

`unverified` is the only assessment issued for successfully stored submissions.
`failed` describes persistent storage/integrity failure, not failed project tests.
`verified` is reserved for a future trusted verifier; Schema requires a verifier
reference, but a syntactically valid reference is NOT an authorization proof.
This milestone has no verified issuer, promotion API or trust-by-content path.
An uploaded JSON `{"verified":true}` stays unverified even after a real controller
command exits 0. A matching SHA-256 proves byte integrity only. Schema validation
of an arbitrary external document does not make that document trusted evidence.

The immutable metadata is stored with its schema version in the DB and protected
by a separate SHA-256 over canonical version+metadata JSON. Content SHA covers
the exact raw bytes. Lifecycle changes never rewrite immutable metadata or bytes.
The unkeyed hashes detect mismatches against a trusted DB; they are not signatures
and do not defend against an administrator rewriting both metadata and hashes.

## Usage

Apply migration `0005` to a development/test DB first. Store root must be a stable
private absolute directory outside repositories and workspace roots; do not change
it for an existing DB without an explicit storage migration/restore procedure.

```python
from pathlib import Path
from hogwarts.artifacts.contracts import ArtifactStore, ArtifactUpload
from hogwarts.artifacts.store import LocalArtifactStore

# Operator configuration and Principal come from trusted application code/M1 auth.
store: ArtifactStore = LocalArtifactStore(workspace_manager, Path(private_store_root))
evidence = store.put(
    worker, run_id, workspace_id, sandbox_id, lease_token,
    "report-attempt-1", ArtifactUpload(kind="report", media_type="application/json"),
    b'{"status":"agent claim only"}',
)
artifact_id = evidence.artifact.artifact_id
payload = store.read(worker, run_id, artifact_id, lease_token)
historical = store.read(owner_human, run_id, artifact_id)
metadata_only = store.describe(owner_human, run_id, artifact_id)
```

Put requires an active assigned M1 worker, matching original lease, READY Workspace
and matching Sandbox. Existing GitWorkspaceManager authorization is reused without
changing its rules. Metadata is bound to existing DB provenance; this store does
not independently collect/validate a source diff or claim uploaded bytes came from
that sandbox. That would require a later trusted collection/verification phase.

Worker reads also require an active original lease; it is rechecked after IO so an
expiry during read cannot return content. The owner human may read history after
lease expiry, cancellation or Workspace/Sandbox cleanup. Reads always require run
and artifact IDs; there is no read-by-path or read-by-content-hash interface.
`describe` validates DB metadata only and reports `integrity=not_checked`; it does
not silently claim bytes were just verified. Successful read hashes all bytes before
returning anything. Unsafe/missing/tampered content never returns a partial payload.

Allowed kinds: report/patch/log/binary. Media types: text/plain, application/json,
application/octet-stream. The service does not parse or execute uploaded content.
Limit: 16 MiB/artifact, actual immutable bytes only. No stream/path imports or rendered
HTML responses. IDs are server UUIDs; only canonical UUIDs can address stored objects.

## Idempotency and lifecycle

Migration 0005 adds artifact_metadata with run/workspace/sandbox foreign keys,
owner/producer/lease hashes, unique (run, producer, hashed idempotency key), request
hash, content SHA/size, versioned immutable metadata/hash, lifecycle version and error.

PENDING intent + WorkflowEvent commit before filesystem effects. An SQL write lock
serializes each artifact across Store instances/sessions. Exclusive create writes
`objects/<server-uuid>.blob`, flushes/fsyncs bytes, applies read-only mode and (Linux)
fsyncs the directory. READY + Audit commit only after full integrity verification.
There is no overwrite/update/delete API. Equal bytes under distinct keys get distinct
IDs; no cross-run content deduplication or hash-based access is introduced.

Exact retry returns the original document/ID after checking metadata and bytes.
Changing bytes, type, media or bindings conflicts. DB/storage are not atomic: after
a final DB commit failure, PENDING may coexist with a complete file. Exact retry
can adopt only matching regular single-link bytes and re-establish fsync/read-only
mode. Windows briefly restores write permission solely to obtain a FlushFileBuffers
handle; it never writes content. Partial/mismatched files are rejected, not repaired.

Storage write/fsync failure -> FAILED; metadata/hash/path/content inconsistency ->
CORRUPT. These states are terminal for publishing that key, retained for inspection.
New key can create a new artifact. Corrupt metadata is not returned by describe.
A transient read IO error returns a stable 503/audit without inventing proof of
corruption. No auto-delete, garbage collector, repair loop or M2.4 reconciler.

## Filesystem security

All names are service-generated UUIDs; root is operator-only configuration.
Validate ancestor directories/reparse points and object lstat/fstat identity, type,
single hardlink count, length, stat changes and SHA. Use O_EXCL, O_NOFOLLOW where
available, bounded reads and nonblocking opens after rejecting special files.
POSIX requires service-owned root/objects with no group/other permissions; files
become 0400. Windows checks links/reparse points and uses read-only attributes;
inherited host ACLs and service ownership must be protected by the operator.

This prevents the supported path/link/overwrite cases, not hostile same-UID/admin
filesystem races. There is no WORM hardware, encryption at rest, general secret
scanner, total store quota, retention policy or backup/restore implementation.
Use a private service directory on a dedicated, capacity-controlled filesystem.
The trusted LocalObjects backend is not an Agent tool and must not be exposed
directly; authorization lives in LocalArtifactStore before any object access.

## Audit and failures

Reuse WorkflowEvent schema v1 with source=artifact_store, aggregate_id=run_id,
per-artifact monotonic transition versions and artifact ID payload. Events include
pending/ready/replayed/read/described/failed/corrupt/denied. No token, raw key,
content, raw exception/DB stderr or caller path is logged. IDs are opaque metadata,
not credentials. Lifecycle state and its event commit in the same DB transaction.

Before-intent DB failure has no file effect. After-file commit failure leaves
recoverable durable intent. DB failure itself may prevent Audit persistence:
return `artifact_database_and_audit_unavailable` when the follow-up audit also fails;
do not claim it was saved. No independent durable audit fallback is implemented.

## Verification

Use Python 3.12, existing requirements.lock, Git, dedicated TEST_DATABASE_URL and
the M2.2 approved deterministic image. Never use production DB/storage as fixtures.

```text
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
python -m pytest tests/test_artifacts.py -v --junitxml=test-results/artifacts.xml
python -m pytest -v --junitxml=test-results/full.xml
```

For REAL M2.2 regression/Artifact association tests, run on a trusted Linux host
with Docker and HOGWARTS_SANDBOX_IMAGE set to the approved exact local image ID.
Windows is a development host only; Linux runtime, native links or FIFO may skip.
Report skips explicitly. Existing migration tests exercise both DBs up/down/up and
Alembic drift. See the dated acceptance report for actual counts, failures and logs.

No M2.3.2–M2.3.5, M2.4, real Codex, QA/verifier, commit/push/merge or deployment is
implied by this storage milestone. Remote CI remains unverified without a push/run.
