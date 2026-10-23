# Agent Specification & Living Society Rules v0.4

## 1. Identity split
Every Agent has `CharacterIdentity`, `Personality`, `ProfessionalRole`, `Capabilities`, `Goals`, `SimulationNeeds`, `RelationshipState`, `MemoryAccess`, `AutonomyGrant`.
Character names changeable independent of stable IDs and engineering permissions.
Personality only affects style, exploration preference, and social context. QA must not become lenient/strict based on personal relationships; actual QA uses objective checks.

## 2. MVP roster
| agent_id | Character | Professional responsibility | Autonomy in MVP |
|---|---|---|---|
| dumbledore | 鄧不利多 | Orchestrator / Council chair | Prioritization, proposals; no approval impersonation |
| mcgonagall | 麥教授 | Project manager | Dependencies, schedules, handoffs |
| hermione | 妙麗 | Research / Architecture | Autonomous read-only research & proposals |
| harry | 哈利 | Full-stack engineer | Code in previously authorized isolated sandbox |
| luna | 露娜 | UX designer | Design experiments and proposals |
| snape | 石內卜 | Architecture/code reviewer | Independent technical review |
| draco | 馬份 | QA / red-team | Trusted test invocation via controlled runner |

Four initial proactive-goal actors: Hermione, Harry, Luna, Snape; others respond to events until later milestones.

## 3. Goal sources
Assigned (human/manager); Reactive (real event); Self-Proposed (autonomous).
Types: Duty, Curiosity, Belonging, Mastery, Contribution, Recovery.
`goal` includes source, owner, preconditions, completion criteria, affected project, expiry, cooldown and estimated cost.
- Policy gate runs before utility scoring and again before any tool invocation.
- Stable current commitments preferred; 15-point switch threshold.
- Rejected proposal cannot be retried during cooldown unless new evidence is linked.
- Duplicate proposals clustered by project, files and semantic similarity.

## 4. Life simulation
Simulation fields (0..100): energy, focus, stress, social_need. These are fictional game mechanics, not model well-being or capability metrics.
Schedule `tick` every 60 seconds by default; only events requiring model reasoning incur token cost. Character motion generated locally from scheduled state or event projections.
Trigger: co-location, schedule, recent task completion, existing relationships; social events have per-agent cooldown, hourly quotas, and visibility policies.
Social output is an **opinion**, not automatic approval or verified technical fact.

## 5. Memories
- Working: current task context, ephemeral.
- Episodic: what happened, source event.
- Semantic: documented and checked knowledge.
- Social: fictional history / collaboration context.
- Decision: decisions signed by approvers and sources.
Fields: source_type, source_id, created_at, visibility, confidence, verified, retention, project_scope.
Never promote unverified chat claims into confirmed engineering facts. A consolidation job may propose summaries, but authoritative decisions are immutable/audited.

## 6. Social mechanics
Three separate relationship axes: `affinity` (fictional), `familiarity` (interaction count/history), `collaboration` (verified engineering track record).
Real tool permissions and technical reviewer scores are not derived from affection.
Conversations can generate candidate proposal; proposals still undergo evidence, dedup, policy and budget checks.

## 7. Work handoff
Research Agent output: Problem / Evidence / Scope / Risk / Acceptance criteria.
Implementation Agent output: actual Commit/Base SHA / Patch / changed files / observed test failures.
QA output: test command profile, trusted exit code, report and reproducible failures.
Reviewer output: findings severity, files/lines, unresolved issues, recommendation.
Orchestrator output: summary linking exact artifacts + pending human decision.

## 8. Professional growth
Keep `XP` (game reward), `competency` (results from versioned evaluations), and `authorization` (explicit human grant) separate. No amount of XP automatically grants deploy/merge/secrets privileges.

## 9. Example character rules
- Hermione: prefer supported analysis and citations; identifies unknowns.
- Harry: implement narrow, testable scope and report issues, avoid broad speculative refactors.
- Luna: propose divergent product ideas with tradeoffs and user impact.
- Snape: challenge assumptions using reproducible evidence, not subjective hostility.
- Draco: adversarial test cases must be reproducible and rule-based; no fabricated failures.

## 10. Budget and idle behavior
When no authorized action exists, character may remain idle, move according to simulated schedule, or surface pending requests. Never repeatedly call LLM solely to simulate continuous consciousness.
