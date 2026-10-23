# M1 API v1

Live Swagger: `/docs`. Machine schema: `/openapi.json`; checked-in snapshot:
`contracts/openapi.v1.json`. API health verifies database connectivity.

All v1 routes require `Authorization: Bearer <token>`. All mutation commands require
`Idempotency-Key` (1–100 ASCII letters/digits/underscore/dot/colon/hyphen). Every
proposal mutation and run claim/execute requires `expected_version >= 1` in JSON.
Get current version from proposal detail after a successful command. Idempotent
replays return their original response even if the aggregate has advanced.

| Method and path | Role | Behavior |
| --- | --- | --- |
| GET `/health` | Public | Database connectivity |
| POST `/api/v1/projects` | Human | Explicit repository/path grant; preapproved defaults false |
| POST `/api/v1/agents` | Human | Agent registration and reservation cap |
| GET `/api/v1/agents` | Both | Scoped agent identities |
| POST `/api/v1/prototypes` | Both | Validate and create PROPOSED proposal |
| GET `/api/v1/prototypes` | Both | Scoped list; limit 1–100, nonnegative offset |
| GET `/api/v1/prototypes/{id}` | Both | Current document, state and version |
| PATCH `/api/v1/prototypes/{id}` | Both | Versioned revision before queueing; invalidates approval |
| POST `/api/v1/prototypes/{id}/start` | Both | Policy check; approval request or atomic queue/reservation |
| POST `/api/v1/prototypes/{id}/cancel` | Human | Cancel eligible proposal, release active reservation |
| GET `/api/v1/prototypes/{id}/approvals` | Both | Revision-bound approval records |
| POST `/api/v1/approvals/{id}/decision` | Human | APPROVED / REJECTED, version and expiry enforced |
| POST `/api/v1/runs/{id}/claim` | Worker | Atomic 5-minute lease, compare proposal version |
| POST `/api/v1/runs/{id}/execute` | Worker | Fake-only execution; current version and lease_token required |
| GET `/api/v1/prototypes/{id}/runs` | Both | Durable run status and explicitly fake evidence |
| GET `/api/v1/prototypes/{id}/events` | Both | Versioned audit envelopes conforming to event schema v1 |
| POST `/api/v1/prototypes/{id}/decision` | Human | ACCEPTED / REJECTED / DEFERRED after fake review |
| GET `/api/v1/merge-requests` | Human | Internal intents awaiting separate approval/executor |
| POST `/api/v1/prototypes/{id}/merge` | Both | Always 403 |
| POST `/api/v1/prototypes/{id}/deploy` | Both | Always 403 |

Errors: 401 absent/invalid credential; 403 unauthorized action/path; 404 missing or
inaccessible resource; 409 stale version/transition/lease/approval, budget or quota,
idempotency conflict; 422 invalid JSON/schema/header; 503 unconfigured credentials.
Validation responses are FastAPI standard errors; domain errors are `{"detail":"code"}`.

## Minimal commands

Create an agent as human:
```json
{"key":"engineer","display_name":"Engineer","budget_cents":100}
```

Create a project as human (declarative grant only; no repository is opened):
```json
{"repo_ref":"https://github.com/example/authorized.git","allowed_paths":["src","tests"],"preapproved":true,"target_branch":"main"}
```

Create a proposal with returned project/agent IDs:
```json
{
  "project_id":"<project-id>",
  "author_agent_id":"<agent-id>",
  "origin":"assigned",
  "title":"Small prototype",
  "problem":{"description":"Example evidence","evidence":[]},
  "prototype":{
    "base_commit":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "allowed_paths":["src/example.py"],
    "max_runtime_seconds":60
  },
  "acceptance_criteria":["Reproducible behavior"],
  "estimate_cents":10
}
```

Start JSON: `{"expected_version":1}`. Preapproved start returns QUEUED/version 3
and a run ID. Worker claim uses version 3 and returns version 4 plus lease token.
Execute uses version 4 and that token. Final review uses the returned current
version (normally 10). Execution first records `sandbox.requested` with a version
CAS, then records `sandbox.ready` only after allocation succeeds. If the project is
not preapproved, start instead returns
AWAITING_EXECUTION_APPROVAL/version 3 and approval ID, with `run_id:null`.
Human approves with that version, then calls start with the newly returned version.

For a queued proposal, the one-shot helper performs claim and fake execution:
```powershell
python workers/engineering/worker.py --proposal-id <proposal-id>
```
It reads WORKER_API_TOKEN, does not poll autonomously, and performs no project
commands. Retries using the same explicit API idempotency key can recover a lost
response. The helper generates fresh keys; inspect durable run status after an
interrupted helper invocation rather than starting another worker blindly.

## Contract versioning

HTTP commands use Pydantic schemas in `/api/v1` and generated OpenAPI. The original
`prototype.schema.json` remains the design document envelope: `proposal_id` is the
generated response `id`, `created_by` maps to `author_agent_id`, and proposal
content maps to `document`. `estimate_cents` is an HTTP governance command field.
HTTP uses a full 40-character lowercase SHA-1 rather than the envelope's looser
minimum length. Actual audit envelopes are tested against the existing event schema,
including names such as `proposal.created`. The original envelope remains v1.
