# ADR 0003: M1 acceptance fixes

Status: accepted, 2026-10-23. Scope remains M1 with fake adapters only.

## Truthful sandbox events without losing execution exclusion

The execution command first compares the proposal version while still QUEUED,
increments it and records `sandbox.requested`. This database write retains the
transaction lock before any adapter call. Only a successful fake allocation can
transition to SANDBOX_READY and record `sandbox.ready`. Failed allocation transitions
from QUEUED to FAILED and releases the reservation without a readiness event.

Simply moving allocation ahead of the old CAS would allow competing requests to
allocate before discovering a stale version. The new regression suite checks that
two execution requests create exactly one sandbox. The additional truthful request
event increments version once more: the usual review version is now 10, not 9.
Clients must continue using the returned current version rather than a constant.

## One rejection audit path

Transaction handling and IntegrityError replay recovery are inside the same outer
DomainError handler. Both serial request-hash conflicts and conflicts discovered
after a racing unique-key insert roll back and persist exactly one denial event.
Successful duplicate retries still replay the original response without a rejection.
No business schema or migration changes are needed.

## Reject unsupported credentials before constant-time comparison

Configured human/worker tokens must be distinct ASCII values of at least 32
characters. Non-ASCII server configuration returns 503 without revealing the token.
Non-ASCII incoming credentials return 401 with a Bearer challenge, before invoking
the string constant-time comparison. Valid configured credentials retain the
existing role/owner/agent scoping.

## Container readiness

Compose now checks the API's local HTTP `/health` endpoint. `up --wait` therefore
waits for an HTTP response in addition to PostgreSQL and migration dependencies.
This checks database connectivity, not production readiness, schema availability
after startup, real sandbox isolation, or a Codex integration.

The three prior acceptance findings are covered by formal regression tests, with
additional checks for concurrent allocation, serial audit behavior and invalid
server token configuration. Remote CI still requires an authorized Git push.
