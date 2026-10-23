# Implementation Roadmap, Tasks & Acceptance v0.4

## Execution approach
Small verified milestones; fake integrations until contracts are stable. Tech options need local version checks before implementation; don't assume undocumented Codex API signatures.

### M0 — Foundation
Deliver: monorepo skeleton (`apps/web`, `apps/api`, `workers`, `packages`, `infra`, `tests`), Docker Compose, PostgreSQL, Alembic, CI, formatting/lint/tests, root AGENTS.md.
Exit criteria: documented local setup; API health endpoint; DB migration up/down in test environment; unit tests run in CI.

### M1 — Governance Foundation (**first Codex implementation task**)
Deliver: Agent/Project/Proposal/Run/Approval/WorkflowEvent/Budget models; state machine; Pydantic validation; authorization and budget checks; idempotent endpoints; atomic claim; fake CodingRuntime/Sandbox/QA/Artifact adapters; focused integration tests.
Exit criteria: attempts at unapproved merge rejected, duplicate requests deduped, incompatible transitions denied, budget enforced, concurrent worker claim passes.
Not in M1: real Codex execution, browser UI world, real Git sandbox.

### M2 — Room of Requirement Runtime
Deliver: pinned Git snapshot, isolated sandbox runner, resource limits, ephemeral workspaces, artifact checksums, command profiles, cleanup and stale-lease reconciler.
Exit criteria: seeded example repo changes only in sandbox; unauthorized paths blocked; main untouched; cleanup survives failed run.

### M3 — Multi-agent Coding Workflow
Deliver: read-only researcher; implementation adapter; QA runner; independent reviewer; manager orchestrator; run trace and human review bundle.
Exit criteria: 1 end-to-end code patch with actual tests, distinct reviewer evidence, no unapproved merge.

### M4 — Autonomous Society Kernel
Deliver: goal engine/utility, commitments/cooldowns, social events, episodic/decision memory, proposal deduplication, budget-aware world scheduler.
Exit criteria: self-generated evidence-based proposal can become approved-scope sandbox run; restarting scheduler doesn't duplicate jobs; no unbounded social loop.

### M5 — Living Castle UI
Deliver: Next.js+Phaser isometric four-room map, 7 Agent avatars, real event projection, Council / Prototype / Pensieve panels, emergency pause.
Exit criteria: world status matches actual engineering event record; user can inspect artifact/test/approval from scene.

## First demo: Enchanted Task Board
Seed React task board with reproducible test scenario. Hermione finds suspected re-render issue; policy authorizes narrow sandbox scope; Harry modifies file; Draco runs tests; Snape reviews; founder sees Patch & Test Run and accepts/rejects; no automatic merge.
A failure or no performance improvement must remain a valid recorded outcome.

## Suggested test taxonomy
- Unit: scoring, permissions, state machine, schema, cooldown, dedup.
- Integration: DB migrations, concurrent claims, idempotency, budget reservation, event outbox.
- Sandbox: path enforcement, credentials absence, network policy, resource quota, cleanup.
- Workflow/evals: evidence-based proposal, truthful QA reports, agent role consistency.
- E2E: propose -> sandbox -> verify -> founder decision; event UI corresponds to truth.

## Definition of Done for every milestone
Code + migrations + reproducible tests + documentation + exact executed command/results + known limitations + updated decision log. Never declare tests passed without executing them.

## Architecture decisions still OPEN
1. Local Codex SDK vs App Server vs remote Agents API; evaluate actual SDK versions and hosting needs at M3.
2. Single-tenant MVP vs hosted multi-tenant; default to local single-user with future tenant columns.
3. Container vs MicroVM threat model; local trusted repos first, stronger isolation before arbitrary third-party projects.
4. Original-IP replacement plan if project becomes public/commercial.
