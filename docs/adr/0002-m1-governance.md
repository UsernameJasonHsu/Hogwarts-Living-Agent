# ADR 0002: M1 governance boundaries and transaction design

Status: accepted, 2026-10-23.

## Identity and policy

Local single-owner development uses separate human and worker bearer secrets,
loaded only from environment. Tokens must differ and have at least 32 characters.
`OWNER_ID` and `WORKER_AGENT_KEY` are server configuration, never request claims.
Only humans can register agents, grant project scope and decide approvals/reviews.
Worker reads/writes are limited to its configured agent within the configured owner.
Unknown actions are absent or denied; merge/deploy always return 403 in M1.
This is not an internet-ready identity provider or a multi-tenant deployment.

## Durable commands and concurrency

Each successful command stores a unique `(owner, actor, idempotency_key)` plus a
hash of operation, resource and canonical request body. The command transaction
inserts the key first; database uniqueness serializes simultaneous retries. Reusing
a key for another request returns 409. Historical retries return the original
response; fetch current detail separately. Rejected commands are not cached.

Proposal changes use SQL `UPDATE ... WHERE version = expected_version AND status =
old_state`, check row count and increment version. The state machine checks allowed
edges before updating. The run claim adds its own conditional update of QUEUED
status and null lease, returns a random lease token, and increments proposal version.
Claims are authenticated by configured agent; two processes with the same worker
identity still cannot obtain two leases. No in-memory mutex is authoritative.

Queue creation, budget reservation, version updates, idempotency results and audit
events commit atomically. Order is proposal CAS, workspace counter, agent counter.
Workspace cap is 500 cents / two active runs; agent cap is at most 100 cents / one
active run. M1 enforces in-flight reservation caps, not rolling daily spending.
Only pure fake work executes, with actual cost zero. Reservation release and run
completion/cancellation are atomic. Daily paid cost accounting belongs with a real
runtime, before any billable execution is enabled.

WorkflowEvent is the append-only application event log and undelivered outbox.
No dispatcher, world projector or notification sender exists yet. Business writes
and events share a transaction; denied authenticated commands are logged after
rollback in a separate transaction. Event mutation APIs are absent. A future
deployment must separately restrict DB credentials to protect audit storage against
direct SQL tampering; M1 does not claim cryptographic tamper resistance.

## Approvals and fake evidence

Non-preapproved projects enter AWAITING_EXECUTION_APPROVAL without creating a run
or reserving budget. One-hour execution approvals bind action, exact proposal
revision/document hash, agent, project (through proposal), pinned commit and branch.
Human approval cannot expand project paths. Editing a proposal invalidates approval;
start rechecks expiry and revision and consumes a matching grant atomically.

Creation begins at PROPOSED: discovery/research, retries and autonomy are later work.
Only the fake worker advances engineering stages. Fake results explicitly say no
commands ran and never present an exit code or trusted verification. Human acceptance
creates an internal MergeRequest intent with PENDING_SEPARATE_APPROVAL; it does not
authorize or perform a real merge. There is no SCM or deployment executor.

Lease expiry is enforced. Queue and lease records survive API restarts, but expired
lease reconciliation/heartbeats, orphan cleanup and retries are M2 work. Expired
work remains reserved until human cancellation; no unsafe automatic retry occurs.

## Future integration ADR

Codex SDK/App Server selection is deferred to M3. No SDK dependency, undocumented
API call, host credential mount, real repository read/write, or networked coding
adapter is present. M2 must enforce actual symlinks, filesystem scope, resource and
network limits in a genuine isolation boundary; lexical M1 validation is not that
boundary. No M2–M5 features are implemented.

References: SQL CAS follows the constraints described in
https://docs.sqlalchemy.org/en/20/orm/versioning.html. FastAPI bearer extraction:
https://fastapi.tiangolo.com/tutorial/security/first-steps/.
