# ADR 0007 — M2.3.2 Secure Output Import

Status: design and test contract BEFORE implementation, 2026-10-24.

## Existing foundations and scope

Preserve dirty m2-1-git-workspace-manager branch and all existing 415 tests. Keep M1
Governance, M2.1 workspace security, M2.2 default DockerDriver/Controller, M2.3.1
ArtifactStore and Evidence v1 behavior unchanged. Add a new internal OutputController,
managed OutputDockerDriver, SecureOutputImporter protocol/service, strict archive
validator, trusted reader image and migration 0006. No public HTTP endpoint, agent
host-path/archive upload, patch building, QA, Codex, reconciler or repository writes.

## Snapshot boundary

M2.2 /output is tmpfs and the controller stops the producer after execution. Reading
that tmpfs later is not a persistence contract. docker cp also documents restrictions
for tmpfs. New OutputDockerDriver instances therefore use a uniquely owned named
tmpfs-backed /output volume with the SAME 16 MiB/2048-inode/noexec/nosuid/nodev budget.
All other M2.2 controls are inherited and checked unchanged. Original default driver
is unaffected. A pinned, image-owned trusted reader holds this output mounted RO,
so it survives producer stop. Reader UID/GID 10001, network none, read-only rootfs,
cap-drop ALL, no-new-privileges, private namespaces, CPU/RAM/PID/time limits; no socket,
credentials or source mounts. Reader is a trusted controller resource, not sandbox
code. It has an absolute lifetime bounded by the original lease/runtime expiry.

Importer requires committed COMPLETED + known exit 0, stopped producer (no live
descendants), matching original container/volume/profile/input identities and no
unknown container attached to output. Export only after writes cease. Under the
same Sandbox SQL operation lock used by M2.2 destroy, validate before AND after
capture; reader never runs sandbox-provided code. Export a bounded uncompressed
USTAR through fixed argv. Time/output limits are enforced while streaming. Unsafe
output, a running producer, expired helper, missing volume or identity changes fail
closed. A stopped container does not alone prove data persisted: the RO reader
must have held the managed volume, and archive completeness is verified.

## Import contract and paths

SecureOutputImporter accepts only a trusted OutputController object identity,
authenticated M1 worker, Run/Workspace/Sandbox IDs and original active lease. No
agent archive bytes, Host Path, command, output path or Docker arguments. A trusted
OutputGrant binds profile name to a repository-relative target prefix, e.g. src.
Archive prototype.txt then maps to virtual src/prototype.txt. This is a manifest
mapping only; it never writes to a Git worktree. Validate repository identity,
original workspace fingerprint/pinned HEAD, operator/project/proposal Allowed Paths
before snapshot and before publication. Policy change/revocation fails closed.

The exact zero-length regular .executed runtime marker is recognized but not
published as a repository file; all other entries are validated. Limits: 12 MiB
archive/capsule, 512 entries, 256 files, 4 MiB/file, 8 MiB payload. Reject compression,
PAX/GNU longname/sparse extensions, links, special files, noncanonical/absolute/
traversal paths, .git metadata, duplicates/case aliases/file-directory collisions,
bad headers/checksums/padding, truncated/concatenated archives or nonzero trailers.
Parse bounded headers directly; never extract to a filesystem or decompress data.
Preflight the ENTIRE archive before creating ANY artifact. Empty output is valid.

## Immutable provenance, retry and audit

Migration 0006 OutputImport binds run/workspace/sandbox/owner/lease, source/config
fingerprint, controller invocation/container, state/version, private seal key,
snapshot SHA/proof, immutable file mappings and final manifest artifact ID/hash.
Unique sandbox means one stopped-output snapshot per single-execution M2.2 sandbox.
States PENDING -> SNAPSHOTTING -> SNAPSHOT_READY -> IMPORTING -> IMPORTED, with
REJECTED/FAILED/UNCERTAIN. Persist intent before snapshot. SQL write locks serialize
capture with other imports AND sandbox lifecycle operations. Never nest a separate
ArtifactStore write transaction inside a held SQLite import write lock.

Store the captured archive and proof in an exclusive, fsynced private capsule. A
per-import random DB seal key HMAC-binds context/proof/archive; only trusted controller
code can seal it. No key/token/content in Audit or user-visible responses. DB/FS
cannot be atomic: exact retry can adopt only a complete authentic capsule. A
SNAPSHOTTING row without a valid capsule is busy/uncertain, never recaptured blindly.

Publish raw archive, each regular file and a versioned canonical manifest using
M2.3.1 immutable ArtifactStore and deterministic per-import idempotency keys. Multiple
publishers may safely replay the SAME sealed bytes because the store serializes keys;
no duplicate artifacts or divergent manifest can result. Mark IMPORTED only after
all artifacts are READY and cross-check IDs, SHA/length/bindings. A partial failure
never returns a completed bundle; retries use the sealed snapshot, not live output.
Revalidate committed artifacts on replay. Unknown DB/daemon outcomes fail closed.

Keep Evidence v1 source=agent_submission/unverified as its conservative content
classification. The new immutable manifest/OutputImport records provide controller
capture provenance (source container, volume, invocation, input/archive/file hashes,
grant/prefix, capture time). Trusted transport is not trusted content; never promote
to verified. No overwrite of existing artifacts. No mutation of Run.evidence.

Audit reuses WorkflowEvent schema v1, source=output_import, monotonic transition
versions plus stable denial/failure codes. DB outage may also prevent audit storage;
report that honestly. Manual cleanup through the managed driver removes its reader
and output volume after verifying identities; no background reconciler added.

## Files planned

apps/api/hogwarts/outputs/{contracts,archive,capsule,driver,importer}.py;
append OutputImport model; infra/migrations/versions/0006_m2_3_2_output_import.py;
infra/output-reader/{Dockerfile,reader.py}; tests/test_output_contracts.py then
tests/test_output_imports.py; docs/16-SECURE-OUTPUT-IMPORT.md, ADR 0007, acceptance
report. README scoped additions. CI adds reader-image build/opt-in, no CI pass claim.

## Test contract (before implementation)

1. Parser normal/empty/binary/directory, exact SHA; full preflight leaves no artifacts
   on invalid input. Paths: traversal/absolute/ADS/metadata/case/prefix collisions,
   duplicate files/dirs; symbolic/hard links, FIFO/devices, sparse/PAX/longname.
2. Archive bombs/declared huge sizes/too many files/total/file/archive limits,
   malformed octal/checksum/padding/trailer/truncation/concatenation/compression.
3. Controller-only entry, owner/run/workspace/sandbox/lease/grant/prefix/fingerprint
   denials, cancelled/expired execution, no host path/archive injection interface.
4. Stopped-output fixed snapshot; mock mutation and before/after identity mismatches
   rejected; capsule tamper, partial capture, DB finalization failure, concurrent
   imports/restarts and deterministic artifact replay; no overwrite or verified.
5. DB/Storage failures before intent, after capsule, during file/manifest publishing,
   audit outage; uncertain results never cause a new snapshot or completed response.
6. REAL Linux managed-output integration: default verify output, empty cpu output,
   reader non-root/read-only/no-network/mounts/limits, producer stopped, no repository
   changes, import/retry after managed resource destroy, cleanup and failure injection.
7. Full existing 415-case M1–M2.3.1 regressions; SQLite/PostgreSQL migration roundtrip/
   drift; Windows portable contracts/store tests, explicit Linux-only skips.

Limits: trusted patched Docker daemon/service-owned roots/DB; no kernel escape or
hostile same-UID/admin protection. No real power-loss proof, Windows runtime, remote
CI, total retention/GC or new QA semantics unless actually verified separately.

References: https://docs.docker.com/reference/cli/docker/container/cp/ ;
https://docs.docker.com/engine/containers/run/ .

## Implementation notes / acceptance mapping

Actual files: `tests/test_output_contracts.py`, `tests/test_outputs.py`,
`tests/test_output_linux.py`, `tests/test_output_migration.py`,
`docs/16-SECURE-OUTPUT-IMPORT.md`, `infra/migrations/versions/0006_m2_3_2_output_import.py`.
The initial test plan above is retained as a preimplementation record.

Implementation preserves all preexisting M1/M2.1/M2.2/M2.3.1 source and test definitions.
Only the new OutputImport model is appended to models.py; README and CI receive scoped
additions. The new output-capable driver/controller are opt-in, not installed as M1 adapters.

Capture is restricted to committed successful COMPLETED execution. Invalid capture or
archive fails as UNCERTAIN; sealed-object corruption is FAILED. A prior SNAPSHOTTING
intent with no capsule reports busy/uncertain without recapturing or guessing whether
another controller is still active. There is no automatic lease stealing/reconciler.
Publication failure remains IMPORTING with an explicit denied audit; exact sealed replay
is supported. REJECTED is reserved in the lifecycle schema and is not currently emitted.

A sealed snapshot in SNAPSHOT_READY/IMPORTING/IMPORTED can be replayed after the producer
is destroyed, with the same valid Run lease and READY workspace. An initial import after
destroy is denied. This avoids depending on expired reader/tmpfs resources for retries.
Empty output is tested using a trusted test fixture helper that removes deterministic
output after producer stop; the helper exits and is removed before capture. Attack fixtures
use the same test-only mechanism for symlink/hardlink/FIFO/oversize/count attacks. This helper
is not part of the importer API or an Agent-selectable runtime profile.

The Linux container's reader uses bounded in-memory USTAR construction; the controller
uses a separate bounded binary Docker stdout stream rather than DockerDriver's 64 KiB
JSON/log transport. Reader and capture have independent 8s/10s limits. A trusted, pinned
image and trusted Docker daemon/service-owned DB/roots are assumptions, not proven hostile
host isolation. No snapshot bytes are ever evaluated, extracted or applied to Git.

Final test counts, failure history, Linux/Windows distinctions and unverified items are
recorded separately in the acceptance report; a planned check alone is never a pass.
