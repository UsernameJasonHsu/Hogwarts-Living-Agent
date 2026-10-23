# First Codex Mission — M0 + M1 only

Use this file as the development kickoff prompt. Do not implement M2–M5 yet.

## Mission
建立 Hogwarts Living Agent OS 的治理核心。先閱讀根目錄 `README.md`, `AGENTS.md`, `docs/01-PRD.md`, `docs/02-SYSTEM-DESIGN.md`, `docs/04-GOVERNANCE.md` 和 `docs/05-ROADMAP.md`。

### Implement
1. M0 project scaffold: FastAPI, Python 3.12+, PostgreSQL, SQLAlchemy 2, Alembic, Pydantic 2, Pytest, lint & Docker Compose. Keep future Next.js/Phaser workspace as skeleton only.
2. M1 models: Agent, Project, Proposal, PrototypeRun, ApprovalRequest, BudgetReservation, WorkflowEvent.
3. Proposal create/list/detail; schema validation; state machine with optimistic locking.
4. Policy checks and explicit human approval endpoints. Never allow direct merge or deploy.
5. Atomic worker job claim + idempotency key + tests for concurrent duplicate requests.
6. Fake CodingRuntime, FakeSandboxManager, FakeArtifactStore and FakeTestRunner only.
7. At least tests for: unauthorized action, invalid transition, duplicate start, budget exhausted, two worker claims, stale approval and audit event.
8. Document migrations, commands, environment variables (without secrets), and known limitations.

### Constraints
- Do not actually call Codex, run autonomous network actions or modify real user's repositories in M1.
- If official SDK integration is considered, record it as a future ADR pending version and API verification.
- Default deny if policy cannot resolve permissions.
- Avoid expansive features beyond M1 and avoid invented test results.

### Deliver
Architecture summary, completed source, migration files, OpenAPI endpoint documentation, real test output and exact list of remaining work.
