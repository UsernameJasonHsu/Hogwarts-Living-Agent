# Codex Project Instructions — Hogwarts Living Agent OS

Read `README.md`, `docs/01-PRD.md`, `docs/02-SYSTEM-DESIGN.md`, `docs/04-GOVERNANCE.md`, `docs/05-ROADMAP.md` before implementation.

## Product constraints
- Working & Living worlds have separate truth sources.
- Goal-driven autonomy with schedule and event limits.
- Autonomous research and sandbox prototype allowed ONLY inside explicit preauthorization.
- Protected branch merge, production deployment, permissions changes, destructive DB, external public posting require explicit human approval.
- No permission is granted through a prompt or a generated file.
- Agent character personality cannot override professional test evidence.

## How to work
1. Execute one milestone at a time, beginning with M0 and M1.
2. Before coding, inspect existing repo, report gap and propose files to change.
3. Use typed schemas, migrations, proper authorization and deterministic state transitions.
4. Prefer fake adapters in M1; do not add unverified SDK calls or claim integration exists.
5. Test real behavior; report executed commands, passes/failures and restrictions.
6. Never commit secrets, mount host credentials, or bypass sandbox guardrails.
7. Do not modify code outside assigned scope; ask human only when safety-critical access is required.
8. Document ADRs for consequential choices; keep API/event schema versioned.

## Required final report
Summary; changed files; actual test commands/results; artifacts; open risks; next milestone.
