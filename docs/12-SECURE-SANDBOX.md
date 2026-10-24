# M2.2 — Secure Sandbox Runtime (Linux only)

## Supported scope

An internal trusted controller for local, operator-reviewed repositories. M1 HTTP
API and fake engineering adapters remain unchanged. No Codex SDK, repository test
execution, artifact/QA store, automatic merge/push, deployment or background
reconciler. Windows is a development host, not a supported controller platform.
Docker Desktop provides the Linux daemon for local verification; this does not
establish native Windows isolation.

## Trusted configuration and usage

Build the minimal deterministic image from the dedicated context (never repository
root, which could accidentally include sensitive source):

```sh
docker build -t hogwarts-sandbox:local infra/sandbox
docker image inspect hogwarts-sandbox:local --format '{{.Id}}'
alembic upgrade head
```

An operator reviews the image/Dockerfile/runner, then approves its exact local
`sha256:...` image ID in a RuntimeProfile. Tags or agent-supplied image references
are refused. Docker daemon must be Linux with cgroup v2, CPU/memory/PID enforcement
and seccomp. Install the Docker CLI on the trusted Linux controller; it connects
only to `/var/run/docker.sock` with a minimal environment and no Docker credentials.
The daemon/socket grants powerful host authority: keep the controller private and
outside the Agent's process/tool boundary. Never mount that socket into a workload.

```python
from hogwarts.sandboxes.contracts import RuntimeProfile
from hogwarts.sandboxes.controller import LinuxSandboxController
from hogwarts.sandboxes.driver import DockerDriver

# All values below are trusted application configuration, not agent request JSON.
profile = RuntimeProfile("approved-verify", approved_image_id)
controller = LinuxSandboxController(
    existing_git_workspace_manager, DockerDriver("local-controller"), (profile,)
)
workspace = existing_git_workspace_manager.create(worker, run_id, lease_token)
sandbox = controller.create(worker, run_id, workspace.id, lease_token, profile.name)
controller.start(worker, run_id, lease_token)
result = controller.execute(worker, run_id, lease_token, "attempt-1")
controller.stop(worker, run_id, lease_token)
controller.destroy(worker, run_id, lease_token)
```

The worker Principal must originate from trusted M1 authentication. Run must be
CLAIMED, proposal QUEUED, worker assigned and lease active. Workspace must match
the run/owner/original lease and remain READY. Reuse M2.1 grant intersection, Git
provenance, snapshot/link/path checks through its internal manager boundary; no new
authorization subsystem. This tightly coupled bridge is private to the modular
monolith and must be reviewed if M2.1 internals change.

## Isolation and data flow

Controller copies only allowed regular files from the validated worktree into a
bounded in-memory tar. Excludes `.git` and out-of-scope source; denies credential
filenames/directories (`.env*`, `.ssh`, `.aws`, `.docker`, keys, PEM, secrets, etc.).
M2.1 checks links/hardlinks/reparse points, portable paths and concurrent changes.
Snapshot is at most 48 MiB source, 8 MiB/file, 60 MiB archive; source checksum is
persisted and rechecked at start. Generic source-secret scanning is NOT provided:
the operator must approve the repository data; never place secrets in source.

Docker `cp` is incompatible with read-only rootfs on the verified daemon. Instead,
use a named **tmpfs-backed volume** (64 MiB, 12000 inodes, nodev/nosuid/noexec).
The volume is uniquely derived from the durable sandbox UUID. An image-owned
initializer, also UID/GID 10001, network=none, read-only rootfs, cap-drop ALL,
no-new-privileges and bounded resources, accepts the controller tar via stdin.
It independently rejects traversal, links, duplicates, file/count/size violations;
it never runs project code. It writes mode 0444 files/0555 directories and exits.
Its own alarm is bounded by lease expiry/20 seconds. It is removed before commands.
The workload holds the volume mounted **read-only** throughout initialization to
keep the tmpfs mount alive; the workload PID1 is a fixed idle/deadline process.

Workload has non-root UID/GID 10001, read-only rootfs, cap-drop ALL,
no-new-privileges, default seccomp, network=none, private PID/cgroup/UTS, IPC=none,
no published ports/devices, no host mounts, no Docker socket, secrets or host env.
Writable directories: `/output` (16 MiB/2048 inodes) and `/tmp` (8 MiB/1024 inodes),
nodev/nosuid/noexec. `/dev/shm` is unavailable for scratch writing. Standard runtime
device/proc mounts still exist; this is not a microVM or a full syscall allowlist.
Default limits: 0.5 CPU, 128 MiB including no extra swap, 24 PIDs, 8s command and
45s lifetime, all further bounded by proposal runtime and original lease expiry.
File-descriptor/core limits are 128/0; Docker logging is disabled.

Profile commands are image-owned deterministic `verify`, `cpu`, `memory`, `pids`,
`disk`, `timeout`, `fail`. They never execute source scripts, shell text or input
commands. Negative probes exercise only the disposable container. `verify` writes
a scratch prototype in `/output`; it does not edit the source worktree. Output
is a bounded JSON execution result, not a QA verdict or published artifact. There
is no result-to-worktree synchronization in M2.2. Output content is ephemeral and
removed with the sandbox; M2.3 storage is deliberately absent.

## Lifecycle and failure handling

Migration 0004 adds SandboxInstance with immutable run/workspace/lease/profile/input
bindings, deterministic resource identity, expiry, version, invocation/result/error.
CREATING intent commits before daemon effects. SQL write locks serialize operations
across sessions. CREATED -> RUNNING -> EXECUTING -> COMPLETED; failures -> FAILED.
STOPPED/DESTROYED are execution tombstones; CLEANUP_FAILED can be retried. An
identical create/start/stop/destroy is idempotent. Existing partial CREATING state
is cleanup-only rather than implicitly recreating resources.

Execution intent is committed before one approved command. Same completed invocation
returns stored result; conflicts/uncertain EXECUTING are refused without replay.
An image-side exclusive marker provides an additional one-shot guard. After any
execution outcome, the controller kills the whole container to stop descendants.
Timeout errors never imply a background exec was safely cancelled until container
termination succeeds; stop failure records FAILED and requires retry cleanup.
Workload PID1 exits at an absolute deadline independently of controller availability.

Original worker with original token may stop/destroy after lease expiry; owner human
may clean up without token. Changed profiles cannot execute but do not prevent
cleanup. Before deleting anything, validate labels, stored container ID, volume
driver/options, and deterministic resource names. Unknown identities are not removed.
Driver/daemon failures preserve stable codes, retryable cleanup state and events.
Audit uses WorkflowEvent schema v1, source `sandbox_runtime`, per-instance monotonic
transition versions; denial events use version 0. Never log lease tokens, raw Docker
stderr, source contents or command output in audit events.

Docker operations and DB commits are not atomic. After controller/DB crashes,
durable intent may describe partial effects; inspect and explicitly retry destroy.
No automatic recovery or orphan scanner is present (M2.4). The independent deadline
stops processes, but named volumes/metadata may remain until authorized cleanup.
Cancellation/lease revocation during a running command is observed at subsequent
control operations; original expiry and command timeout remain enforced. Immediate
external revocation watchdog is outside this milestone.

## Verification commands

Run these on a trusted Linux development/test host with local Docker and a
dedicated PostgreSQL TEST_DATABASE_URL (never production):

```sh
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
docker build -t hogwarts-sandbox:test infra/sandbox
export HOGWARTS_SANDBOX_IMAGE=$(docker image inspect hogwarts-sandbox:test --format '{{.Id}}')
python -m pytest -v --junitxml=test-results/linux-junit.xml
```

On Windows, omit HOGWARTS_SANDBOX_IMAGE and run the same Python tests with a dedicated
PostgreSQL URL. Real Linux tests skip explicitly; Windows cannot claim those checks
passed. Symlink creation may also be unavailable. Full regressions retain all M1
and M2.1 assertions. Migration tests exercise SQLite/PostgreSQL up/down/up and drift.
CI now builds the deterministic image before opting into real Docker tests; an
unpushed branch has no remote CI result.

## Remaining verification boundary

Evidence must distinguish fake driver logic from real Linux resource enforcement.
No arbitrary hostile third-party workloads, kernel escape resistance, microVM,
user-namespace remapping, SELinux/AppArmor-specific policy, native Windows runtime,
disk inode-exhaustion stress, host reboot, process-crash/DB-outage matrix, immediate
revocation watchdog or remote CI is established by the local tests. Container
isolation shares the daemon host kernel and requires a trusted, patched operator
environment. See the dated M2.2 verification report for actual results.
