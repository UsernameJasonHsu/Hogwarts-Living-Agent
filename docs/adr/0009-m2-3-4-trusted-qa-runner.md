# ADR 0009 — M2.3.4 Trusted QA Runner

Status: architecture and test contract BEFORE implementation, 2026-10-24.

## Architecture / authority

Internal TrustedQARunner.run(principal, run_id, build_id, profile_name, lease_token)
accepts identities only, never command/shell/Docker options, image or host paths.
Operator-created immutable QAProfileRegistry binds exact Linux image ID, fixed argv,
repository-relative test root and CPU/RAM/PID/timeout/lifetime ceilings. Initial MVP
supports Python unittest discovery and fixed image-owned security/resource probes.
Dependencies must be prebuilt into the approved image. No network/package install.
No public API, real Codex, M2.3.5, reconciler, commit/push/merge/deploy.

Reuse M1 authorization, M2.1 grants/provenance and M2.3.3 validated inputs. Require
READY patch journal of the assigned worker, exact build/run/import/workspace/original
sandbox/lease, Base SHA, deterministic patch/proof artifact keys, immutable content
hashes and current grants. Reconstruct the patch again in private data-only Git and
compare complete BuildProof and patch bytes. Never execute repository code on host.
Read only bounded authorized base blobs by Git object ID; verify blob IDs; overlay
exact imported bytes. Materialize a canonical bounded tar of authorized regular files
and hashes. No .git, symlink, submodule, special file, credential-named paths or host
checkout. Out-of-scope base files are intentionally absent: profiles requiring them
must obtain a new explicit grant. Worktree is not a sandbox.

QA owns a separate UUID, not M2.2 SandboxInstance (which is unique per Run). Reuse
unchanged DockerDriver for a trusted input holder/seeder; actual tests run as PID1
in a NEW isolated job container using that RO managed input volume. Holder never
runs tests. Seed helper is gone before job starts. Both containers use pinned image,
UID/GID 10001, rootfs RO, cap-drop ALL, no-new-privileges, default seccomp, network
none, private namespaces, no devices/socket/host mounts/secrets, bounded CPU/RAM/PIDs,
no swap, no core dumps and bounded /tmp,/output tmpfs. Only trusted controller has
Docker socket access. Absolute holder lifetime and controller job deadline are
bounded by lease/proposal/profile. Job also needs an independent absolute deadline
on controller crash: image-owned PID1 supervisor enforces a fixed profile and deadline,
forwards genuine child exit status, and stops descendants. Supervisor is approved
image code, not source/report content; Docker State and supervisor completion must
agree. A malicious test can forge stdout but it is never parsed as authority.

## Execution fact / attestation

Controller records actual fixed argv/profile digest, exact image ID, daemon container
identity, snapshot hash, timestamps, exit code, OOM/timeout/limits and raw bounded
stdout/stderr. Classify pass, fail, timeout, infrastructure_error, unknown_execution.
Only observed command completion may yield pass/fail; ambiguous daemon/start/kill/
controller outcomes yield unknown, never passed or automatically replayed.
No assertion that exit zero proves semantic correctness or adequate test coverage.

Separate QAExecutionAttestation v1 binds Run/worker/Workspace/original Sandbox/new QA
Sandbox/Build/Patch/BuildProof/Base/Profile/Image/input and output hashes/outcome to
trusted controller provenance. Store immutable raw logs and attestation in existing
ArtifactStore using original Sandbox binding for compatibility; explicit QA sandbox
identity belongs in attestation. Evidence v1 remains agent_submission/unverified as
conservative storage classification. Neither original nor QA artifacts are upgraded.
A claim is trusted only through the controller's persisted journal and authenticated
capsule, not by uploading a JSON bearing the same fields. No public signature / offline
third-party authenticity is claimed; DB and service-owned roots remain trusted.

## Durable state / migration

Reuse M1 IdempotencyRecord as typed namespaced qa-runner.v1 journal, unique per
owner/worker/build/profile. Versioned JSON schema replaces need for SQL migration;
Alembic head remains 0006. This deliberately preserves all existing models/migration
regressions. WorkflowEvent source trusted_qa_runner records transitions/failures with
no tokens, seal keys or raw output. Store schema_version=1, context fingerprint,
random per-execution seal key, expiry, resource IDs, state/version and artifact refs.

PENDING -> EXECUTING (committed BEFORE any daemon side effect) -> CAPTURED ->
PUBLISHING -> READY. EXECUTING without authenticated complete capsule is busy/unknown,
never automatic execution retry. Known uncertain observations persist outcome=unknown_execution; they are never executed again.
READY means immutable evidence publication completed, not that tests passed. A missing
result capsule leaves EXECUTING busy/unknown and cleanup-only.
A complete HMAC capsule of the observed result permits publication-only retry after
DB failure; never re-run tests. Serialized SQL intent and unique keys exclude concurrent
execution across service objects. Immutable deterministic artifact keys support partial
publication recovery. Revalidate auth/input before finalization and on historical retry.
Cleanup independently authorized by owner human or original worker/lease even after
expiry; verify every owned resource before delete. No background GC/reconciliation.
DB outage may prevent audit; report unavailable, do not invent successful audit.

## Planned files / preservation

Add hogwarts/qa/{contracts,profiles,inputs,driver,runner}.py, infra/qa/{Dockerfile,supervisor.py},
contracts/qa-execution.v1.schema.json, tests/test_qa*.py, docs/19-TRUSTED-QA-RUNNER.md,
this ADR, README append only. No changes to M1/M2.1/M2.2/importer/patch builder or old
tests/migrations/CI. Preserve initial HEAD and 105 source fingerprints.

## Test Plan (preimplementation)

1. Contracts/registry deny extra command/shell/env/mount/image arguments, invalid
   names/digests/limits/roots, duplicate profiles and mutation. Attestation schema strict.
2. Real Git + SQLite/PostgreSQL + immutable imports/patch builder with fake QA driver:
   normal pass/fail/timeout/OOM/infrastructure/unknown; logs/hashes/identities; source
   unchanged; patch/proof still unverified; forged report never affects classification.
3. Wrong principal/run/worker/lease/workspace/Base, expired/cancelled Run, revoked grants,
   wrong profile, tampered patch/proof/metadata/capsule; sensitive paths/bounded input;
   no daemon calls on denied requests, no host test execution.
4. Concurrent instances exactly one execution, restart/cache replay, profile drift,
   DB intent/result/finalization and storage failure injection; complete sealed result
   recovery only, partial capture/no capsule never replayed; cleanup failure/retry.
5. Real Linux Docker with pinned QA image: actual unittest success/failure, malicious
   forged stdout + nonzero exit, timeout, OOM, PID exhaustion, identity/capability/network/
   readonly/host-path/socket/secrets controls, output cap, deadline and cleanup.
   Controller harness socket is NOT a workload mount. Persist real attestation examples.
6. One complete existing+new regression invocation on each Linux and Windows, separate
   actual counts (not composite dedup counts), Ruff check/format, SQLite/PostgreSQL
   Alembic roundtrip/drift. Save JUnit/raw logs/commands/source hashes/preservation diff.

Acceptance pending execution. Windows is development host only: Linux driver tests
skip there. Remote CI, hostile host/admin, kernel escape, power loss, multi-host stress,
coverage adequacy and production deployment are unverified unless separately tested.


## Implementation / operation (before final verification)

Construct `QAProfileRegistry([QAProfile("python-unit", exact_image_id, test_root="src")])`
in trusted operator code, then `QADockerDriver(controller_id)` on Linux and
`LocalTrustedQARunner(existing_patch_builder, absolute_private_root, driver, registry)`.
Call `run(authenticated_worker, run_id, ready_build_id, "python-unit", original_lease)`.
Only the fixed Python unittest discovery command and image-owned timeout/OOM/PID/
isolation probes exist. Build `docker build -f infra/qa/Dockerfile -t <operator-tag> .`,
inspect the local content-addressed image ID, review it, and register that exact ID.
No dependency installation or network exceptions are available to workload callers.

Attestation records controller ID, exact command and container supervisor argv,
profile/image/input/Patch/BuildProof/Base/result-tree identities, resource limits,
actual daemon timestamps/exit/OOM/timeout, cleanup outcome and immutable stdout/stderr
references. Probe profiles are identified explicitly and are not project correctness
checks. A successful command is not a coverage, semantic-correctness or review claim;
in particular Python unittest can return zero with zero discovered cases. Operators
must review test selection and adequacy. Agent-controlled test code/output cannot
be treated as an independent reviewer.

Env comparison ignores Docker's nondeterministic list order but compares every
name/value and rejects duplicate names. All other security configuration, image,
mount identities and daemon exit observations are checked before/after actual QA.
Output is bounded to 256 KiB per stream during attachment, not after unbounded buffering.
Excess output is infrastructure_error, never pass. Docker logging is disabled for
workloads; raw attach bytes are the saved evidence. Attach errors may have incomplete
output and are unknown, not a successful test result.

Cleanup persists cleanup_requested BEFORE daemon deletion, then audits confirmed
cleanup. A pending request is a tombstone. A racing creator checks revocation before
create/start/execute; subsequent cleanup is idempotent and cannot authorize new tests.
Completed attestation cleanup fields describe that historical observation; later manual
cleanup is separately audited. A missing/partial/invalid result capsule never permits
execution replay. Completed authentic capsules permit publication-only recovery.
All retained objects/journals remain immutable or versioned; no background orphan GC.

No SQL migration is introduced: existing IdempotencyRecord/WorkflowEvent constraints
are reused; new qa-journal.v1 and qa-execution.v1 JSON schemas define typed persistent
state. Existing Alembic head 0006 must pass roundtrip and model-drift checks on SQLite
and PostgreSQL. Original SandboxInstance and ArtifactStore bindings remain unchanged.
QA log/artifact storage still references the original sandbox for Evidence v1; consumers
must use the separate attestation's qa_sandbox_id and trusted journal to identify QA.

Trust assumptions: operator-approved dependency image/profiles, patched trusted Linux
Docker daemon/controller/DB and private roots. No hostile-admin/same-host-user defense,
public-key signature, offline third-party verifier, production readiness or kernel-escape
claim. Limits apply per holder/helper/job container; aggregate capacity is operator managed.
Windows ACL/kernel isolation, remote CI, physical power loss and multi-host failure
recovery remain unverified. No new M1 HTTP endpoints or adapter replacements are added.
