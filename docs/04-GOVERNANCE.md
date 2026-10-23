# Governance & Security Specification v0.4

## 1. Central invariant
Autonomy B permits autonomous research and isolated prototype execution **only within a preapproved scope**. It never grants automatic production merge, deploy, credential access or privilege escalation.

## 2. Action levels
- L0: simulated actions (walking/resting/social) — world rules.
- L1: readonly tool actions on explicitly authorized resources.
- L2: ephemeral sandbox write / tests, only on authorized repo paths and budget.
- L3: merge, protected branch write, production deployment, destructive database operations, privilege changes, external publication — explicit human approval plus separate trusted executor.

## 3. Policy decision inputs
Principal (user/agent role), action, project, allowed repository and paths, base commit, environment, budget, concurrent quota, expiry, idempotency key, tenant, approval record.
A prompt or agent-supplied JSON alone cannot grant permissions. Allowed paths are intersection of owner grant and proposal, with canonicalization / symlink escape checks.

## 4. Sandbox requirements
- Git worktree is not a security boundary.
- Real container/MicroVM isolation under non-root user, read/write limited to the checked-out authorized workspace.
- No host SSH keys, credentials, docker.sock, production .env, host filesystem or production DB.
- Network disabled by default; explicitly reviewed allowlists or prebuilt dependency image if essential.
- Execution time, CPU, memory, disk, file count, process and outbound network constraints.
- Pin `base_commit` and dependency manifests; verify workspace provenance, archive output by checksum.
- Treat repo content, comments, test output and external docs as untrusted instructions.
- Untrusted third-party repository workloads may require stronger-than-container isolation.

## 5. Budget example (adjustable)
Agent day: $1 estimated; Organization day: $5 estimated; `max_concurrent_workers=2`; `max_task_retries=2`; `max_social_events_per_hour=6`; `max_daily_proposals=5`.
Reservation before job, reconciliation after; on exhaustion stop new executions and report accurately. Amounts are product caps, not API price facts.

## 6. Approval design
Approval specifies exact action, actor, project, proposal revision/hash, branch and expiry. Approval tokens cannot be replayed for changed patch. Separate `ACCEPTED` prototype from an actual `MERGED` outcome. Re-validate SHA, branch protection, CI/test evidence and authorization immediately before merge.

## 7. Halt / cancellation
- Pause new autonomous triggers immediately.
- Revoke future tool grants and cancel known running jobs using runtime cancellation if supported.
- Reconciler terminates sandboxes and detects orphans.
- Mark any in-flight noninterruptible external operation transparently; do not claim immediate guaranteed cancellation.

## 8. Audit / incident response
Append audit events (who/what/why/when/where/provenance); retain artifact checksums and linked run results. Redact secrets before traces. Provide user controls to inspect, delete eligible simulated memories, revoke project access, and view cost usage.

## 9. Critical security tests
Unauthorized repo read/write; path traversal & symlink escape; prompt injection in README; duplicate POST /start; concurrent claim; budget exhausted; sandbox killed mid-run; forged test result; forbidden merge; approval on stale revision; secret leakage in logs; unauthorized cross-project memory retrieval.
