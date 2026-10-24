# ADR 0005 — M2.2 Linux Secure Sandbox Controller

Status: accepted implementation design, 2026-10-24. Supersedes no M1/M2.1 behavior.

## Boundary and architecture (before implementation)

Trusted internal Python controller -> fixed Docker CLI argv -> Linux Docker daemon
-> non-root deterministic runner. Windows is a development host only. A Linux
controller may control the Docker Desktop Linux daemon in the verification harness.
Only the trusted controller/harness has daemon access; workload containers never do.
This is local single-operator container isolation, not a hostile multi-tenant boundary.

Reuse authenticated Principal and GitWorkspaceManager authorization/provenance,
Run, Proposal, active Lease and WorkflowEvent. Add SandboxInstance (migration 0004),
an immutable RuntimeProfile registry, Docker driver, controller and deterministic
image runner. Keep M1 Fake Adapters, HTTP API and M2.1 implementation unchanged.
No Codex, artifact/QA store, background reconciler, merge, deployment or push.

An operator approves an exact local content-addressed image ID, fixed command
profile and bounded CPU/RAM/PID/time limits. Agent input is only profile identifier,
run/workspace IDs and lease token. No caller command, env, mount, image or Docker
options. Profiles and controller configuration are trusted code, not agent JSON.

Create validates M2.1 fingerprint, READY state, lease hash, repository identity,
pinned HEAD and current filesystem. Copy only regular files inside allowed paths;
no .git metadata, special files, links, known credential filenames or host paths.
Serialize a bounded, root-owned tar with normalized paths/modes. Docker receives
the tar through stdin; no bind mounts and no daemon-visible host paths are needed.
Snapshot checksum binds input provenance. Secrets embedded in arbitrary source
cannot be detected universally: authorized source must be operator reviewed.

Container: UID/GID 10001, read-only rootfs, all capabilities dropped,
no-new-privileges, default Docker seccomp, network=none, private PID/IPC/cgroup/UTS,
no ports/devices/host namespaces, no volumes/socket/credentials/env inheritance.
Input is root-owned mode 0555/0444 inside size-limited noexec/nosuid/nodev tmpfs;
only /output and /tmp are writable bounded tmpfs, with inode limits. Source Git
worktree is never mounted or modified. M2.2 produces scratch prototype output only;
copying results back or real artifact publication requires later reviewed design.

## Lifecycle and failure

CREATING intent commits before daemon allocation; deterministic UUID name and
controller/run/workspace/profile labels identify only owned containers. Verify full
container configuration before use, and label/ID ownership before stop/destroy.
CREATED -> RUNNING -> EXECUTING -> COMPLETED, or FAILED; STOPPED and DESTROYED
are terminal for execution. Repeated create/start/stop/destroy returns stable state.
Destroy failure is retryable. Existing partial CREATING/EXECUTING is fail-closed:
cleanup is allowed but commands are never blindly replayed. No background recovery.

One approved command per sandbox, with explicit invocation ID and durable execution
intent before execution. Identical completed retry returns bounded stored result;
different invocation conflicts. Serial SQL write locks prevent duplicate effects.
Container PID1 has an independent lifetime cap bounded by lease expiry and proposal
runtime; each command also has timeout bounded by remaining lifetime and lease.
Timeout/failure stops the whole container; no orphaned exec allowed to keep running.
Cleanup permitted to original worker with original lease even after expiry, or owner
human. Audit source=sandbox_runtime, stable codes, no token/raw stderr/output secrets.
Docker/DB are not atomic; uncertain operations remain cleanup-only. Controller crash
cleanup is manual; PID1 lifetime limits persistence until M2.4 exists.

## Files planned

apps/api/hogwarts/sandboxes/{contracts,driver,controller}.py; append model;
infra/migrations/versions/0004_m2_2_sandbox.py; infra/sandbox/{Dockerfile,runner.py};
tests/test_sandboxes.py; docs/12-SECURE-SANDBOX.md; this ADR; verification report.
Update CI to build approved deterministic image and run real Linux isolation tests.

## Acceptance plan (before implementation)

1. All M1/M2.1 regressions unchanged, both SQLite/PostgreSQL migration roundtrips.
2. Unit/profile tests: unsupported platform, unsafe image/profile/limits, argv-only,
   no caller options; reject secrets/links/oversize snapshot; error redaction.
3. Controller tests: full lifecycle, retries, concurrent create/execute, wrong owner,
   wrong token, expired lease, cancelled run, mismatched workspace/run, modified
   provenance, tombstones, post-expiry cleanup, audit and failures at each daemon step.
4. REAL Linux Docker tests (explicit opt-in; otherwise skipped): inspect all controls;
   non-root/CapEff/NoNewPrivs, readonly source/rootfs, absent host sentinel/keys/socket,
   denied network and writes outside scratch, tmpfs quota, PID exhaustion, memory
   allocation denial, CPU cgroup quota and observed throttling, command timeout and
   stopped descendants, bounded lifetime, concurrent retries, cleanup after failures.
5. Report Windows skips separately. Fake driver tests are controller logic evidence
   only and cannot establish kernel enforcement. No unexecuted check marked passed.

References: https://docs.docker.com/engine/containers/run/ ;
https://docs.docker.com/reference/cli/docker/container/create/ ;
https://docs.docker.com/reference/cli/docker/container/cp/ .


## Verified daemon-driven design amendment

Docker cp to a container with read-only rootfs fails on the local Linux daemon.
Keep read-only rootfs. Replace the proposed input tmpfs/cp transport with an owned
Docker named tmpfs volume, initialized through bounded stdin by an image-owned
non-root helper. Hold the workload's read-only volume mount during initialization;
no command runs until seeding finishes and the helper is removed. Input files are
owned by UID 10001 with read-only modes and a kernel-enforced read-only workload
mount. The helper has no host mounts/network/capabilities and an independent alarm.
Track deterministic helper/volume names and validate identity before cleanup.
IPC is disabled, including writable /dev/shm. PID1 uses an absolute expiry so delayed
start cannot extend the original lease. See docs/12 for complete implementation.
The initial test plan above is retained as historical preimplementation evidence;
only the final verification report establishes actual pass/fail status.
