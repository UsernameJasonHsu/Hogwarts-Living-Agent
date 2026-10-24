# Secure Output Import — M2.3.2 operations

Implementation: `hogwarts.outputs.contracts.SecureOutputImporter`, `LocalOutputImporter`,
`OutputController`, `OutputDockerDriver`, `CapsuleVault` and a bounded USTAR parser.
Architecture and original test plan: ADR 0007.

## Trusted configuration

Only trusted service code constructs `OutputDockerDriver(controller_id, reader_image_sha256)`,
RuntimeProfiles, RepositoryGrants, OutputGrants and service-owned artifact/capsule roots.
Use a separately built and pinned `infra/output-reader` image. No Agent tool/API can
supply archives, host paths, runtime arguments, Docker settings or controller identity.
Windows is a development host; the controller runs on Linux with a trusted Docker socket.
That socket is confined to the controller, never mounted in producer/reader containers.

Create/start/execute the approved profile with `OutputController`; after successful execute
commits COMPLETED and stops the producer, call:

```python
result = controller.import_outputs(importer, actor, run_id, workspace_id, sandbox_id, lease_token)
assert result.assessment == "unverified"
```

`OutputGrant(profile_name, "src")` maps raw relative output names to virtual `src/...` targets.
Both original repository grant and current project/proposal scope are checked again.
No mapped path is written to the repository or workspace. Only immutable ArtifactStore
objects are created: raw archive, each validated file, then a version-1 manifest.
The existing Evidence v1 `agent_submission` source and `unverified` status are retained.
The manifest carries controller capture provenance separately from content assessment.

## Capture and durability

A named tmpfs output volume is capped at 16 MiB/2048 inodes, writable only by the producer.
A non-root reader holds a read-only mount with the same absolute lifetime as the producer.
Capture requires a stopped, unpaused producer, a correctly configured reader and exactly
those two attached containers. The trusted image-owned reader emits USTAR; producer output
programs are never executed. Docker identity/configuration proofs must match before and
after capture. Hostile Docker administrators or same-UID service compromise are outside
this boundary; worktrees are not sandboxes.

Capsules are exclusive, fsynced and authenticated with a per-import DB key. Capture intent
is committed first. An absent capsule for a prior SNAPSHOTTING intent returns busy/uncertain;
no blind recapture occurs. Partial/invalid capsule, invalid archive or failed capture fails
closed. A fully authenticated capsule can be adopted after a DB commit failure. SQL locks
serialize capture across processes on the sandbox row and import row. Artifact publication
uses independent transactions and deterministic keys, so simultaneous publishers converge.

Publication failure leaves IMPORTING with a denied audit event; exact sealed replay may
retry. Earlier immutable files may remain unverified if a later operation fails; no successful
import response is issued until all artifacts and final manifest commit. An IMPORTED replay
revalidates every object. A sealed import may replay after sandbox destroy while its Run lease
and READY workspace remain valid. Fresh capture after destroy is forbidden. Expired leases
cannot import/replay; owner-human historical reads use the existing ArtifactStore API.

Limits: plain USTAR only, 12 MiB capsule/archive, 256 files including the control marker,
512 entries, 4 MiB each, 8 MiB total payload. No decompression, extraction, extension records,
links, special files, duplicate/ambiguous paths or repository metadata. Only a zero-length
regular root `.executed` marker is excluded from file artifacts. Resource/path validation
finishes before any artifact publication.

Destroy verifies ownership before removing producer/input volume/reader/output volume.
Lifetime expiration stops containers but does not garbage-collect Docker metadata or capsules.
Operator retention policy, orphan reconciliation, real power loss, kernel escape testing,
remote CI, patch building and QA verification remain outside this milestone.

Migration 0006 only adds `output_imports`; isolated migration tests downgrade to 0005,
upgrade to head and check metadata drift on SQLite and PostgreSQL. Never downgrade a live
production DB using these test commands.

Run `ruff check .`, `ruff format --check .`, and `pytest -v --junitxml=test-results/junit.xml`.
Set TEST_DATABASE_URL to an isolated DB. Linux integration also needs pinned
HOGWARTS_SANDBOX_IMAGE and HOGWARTS_OUTPUT_READER_IMAGE. Without them those tests skip.
