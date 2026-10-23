# System Design — Hogwarts Living Agent OS v0.4

## 1. Topology
- `apps/web`: Next.js + TypeScript + Phaser 2D isometric viewer + side panels.
- `apps/api`: Python 3.12+, FastAPI, Pydantic, SQLAlchemy 2, Alembic.
- `workers/scheduler`: goal planner and world scheduling.
- `workers/engineering`: sandbox allocation, coding adapter, QA tool, artifact collection.
- `postgres`: system of record; optional pgvector for memory semantic retrieval.
- `redis`: ephemeral queue/locking with persisted job records in PostgreSQL. Redis data loss must not lose authoritative tasks.
- `object storage`: diff, trace, logs, reports, assets with access controls and retention.

**Architecture:** modular monolith with workers, NOT microservices at MVP. Codex is a replaceable `CodingRuntime` adapter; this document does not assume a specific SDK signature.

## 2. Bounded contexts
`identity`, `projects`, `agents`, `goals`, `prototypes`, `governance`, `workflows`, `execution`, `events`, `memory`, `world`, `social`, `artifacts`.

Cross-module write operations go through application services, not direct DB mutations or agent tools.

## 3. Main workflows
### Autonomous research to prototype
`world/event -> goal candidates -> memory retrieval -> policy prefilter -> deterministic utility ranking -> research (read-only) -> proposal schema validation -> authorization -> enqueue -> lease -> sandbox create pinned commit -> coding adapter -> patch scope validator -> trusted tests -> independent review -> present -> founder decision -> merge request`.

### Event flow
`tool or business transaction -> transactional outbox -> dispatcher -> event log -> projections (UI, memory, notifications)`.
Business state changes and outbox insertion should be atomic. Consumers use event IDs/idempotency keys and replayable sequence checkpoints.

### Source-of-truth hierarchy
- Trusted test runner/SCM/approval DB: engineering facts.
- Agents: proposals, analysis and opinions; cite sources, not authority.
- World simulation: fiction and UI presentation; cannot declare test success.

## 4. Prototype state machine
`DISCOVERED -> RESEARCHING -> PROPOSED -> POLICY_CHECK -> QUEUED -> SANDBOX_READY -> IMPLEMENTING -> TESTING -> REVIEWING -> AWAITING_HUMAN_REVIEW -> ACCEPTED/REJECTED/DEFERRED`.
Additional terminal or interrupting states: `BLOCKED`, `FAILED`, `CANCELLED`, `EXPIRED`.
- POLICY_CHECK may route to `AWAITING_EXECUTION_APPROVAL` for out-of-scope prototypes.
- Failed tests may create a **new attempt** and return to IMPLEMENTING (max 2 retries); never mutate historical attempts.
- `ACCEPTED` creates a separate Merge Request, never an automatic merge.
- every transition: expected version check, actor/principal, source event, timestamp, reason.

## 5. Candidate selection
Authorized(goal) AND has_budget AND dependencies_ok AND schedule_ok AND cooldown_ok AND concurrency_ok. Then compute
`utility = .30*priority + .20*duty_match + .15*motivation + .15*contribution + .10*schedule_fit + .10*novelty` on 0..100 values.
Existing goal stays in place unless challenger beats it by >=15 points (except critical human cancellation/safety event).

## 6. Core relational schema (logical)
- agents(id PK, key unique, display_name, role, house, persona_version, autonomy_level)
- agent_states(agent_id FK, location, simulated_energy, simulated_focus, simulated_stress, updated_at, version)
- goals(id PK, agent_id, origin, priority, kind, status, cooldown_until, project_id, created_at)
- projects(id PK, owner_id, repo_ref, allowed_paths, access_policy, created_at)
- proposals(id PK, project_id, author_agent_id, status, base_commit, scope_json, acceptance_json, version, idempotency_key)
- prototype_runs(id PK, proposal_id, attempt_no, worker_lease_id, status, sandbox_id, started_at, ended_at)
- artifacts(id PK, run_id, type, uri, checksum, size_bytes)
- test_runs(id PK, run_id, runner, command_profile, status, report_uri, started_at)
- approvals(id PK, proposal_id, action, revision, decision, approver_user_id, decided_at, evidence_hash)
- merge_requests(id PK, proposal_id, base_commit, target_branch, status, approval_id)
- workflow_events(id PK, aggregate_id, aggregate_version, event_type, source, occurred_at, payload_json, idempotency_key)
- memories(id PK, agent_id, type, source_id, verified, confidence, visibility, content, embedding_ref)
- relationships(agent_a, agent_b, affinity, familiarity, collaboration_stats)
- budget_reservations(id PK, scope, estimate, actual, status, run_id)
- sandbox_instances(id PK, run_id, provider, status, created_at, expires_at, cleanup_attempts)

Apply tenant/owner scoping and least-privilege DB access from the start.

## 7. Service interfaces (own abstractions)
```python
class CodingRuntime:
    async def execute(self, sandbox, task, run_id): ...
class SandboxManager:
    async def create(self, repository, commit, limits, policy): ...
    async def destroy(self, sandbox_id): ...
class PolicyEngine:
    async def authorize(self, principal, action, resource, context): ...
class ArtifactStore:
    async def put(self, kind, bytes_, metadata): ...
class TestRunner:
    async def run(self, sandbox, command_profile): ...
```
**M1** uses fake adapters; **M2** uses real sandbox and Git; **M3** introduces Codex integration. Do not assume SDK availability or API parameter names: verify installed official version in implementation.

## 8. Concurrency and recovery
- Use DB row lock/CAS on proposal status/version to claim a job; persist lease expiry and heartbeats.
- A reconciler marks expired lease as recoverable and checks sandbox state.
- Sandbox may be reclaimed by external service if worker crashes; TTL plus cleanup queue.
- Avoid duplicate effects via idempotency key and unique constraints.
- Test failure never bypasses independent review.
- Merge workflow rechecks branch head / patch applicability / protected branch checks at time of approval.

## 9. APIs (proposed, versioned)
`POST /api/v1/projects`; `GET /api/v1/agents`; `GET /api/v1/world/state`; `WS /api/v1/world/stream`; `POST /api/v1/prototypes`; `GET /api/v1/prototypes/{id}`; `POST /api/v1/prototypes/{id}/start`; `POST /api/v1/prototypes/{id}/cancel`; `GET /api/v1/prototypes/{id}/diff`; `GET /api/v1/prototypes/{id}/tests`; `POST /api/v1/prototypes/{id}/decision`; `POST /api/v1/merge-requests`.
Write endpoints require auth, authorization, optimistic version, idempotency for creates/transitions.

## 10. Observability
Record: trace_id, run_id, agent_id, task_id, model/runtime version, prompt template version, wall time, token estimate/actual if available, budget estimate/actual, runner exit code, artifact checksum, approval actor. Secrets/redacted personal data must not be logged.
