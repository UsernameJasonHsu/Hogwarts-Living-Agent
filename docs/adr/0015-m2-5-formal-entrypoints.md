# ADR0015 status clarification — historical decision retained

The original M2.5 preimplementation inspection/decision below is preserved verbatim.
Statements such as “deployed Docker CMD still targets M1” describe that inspection
time, not current deployment. M2.5 canonical startup and formal admission were added.
RC1 further restricts production Worker to signed, pinned operational Linux Gateway
routes; fake-driver regression uses an explicitly in-process nondeployment facade.
See ADR0016 and docs/29-RC1-RELEASE-GATES.md for the subsequent decision.
No original decision fact is rewritten; current validation results are external
evidence, not an inference from this historical design. M3 remains NOT READY.

---

# M2.5 Operational Gate Closure & M3 Readiness Preflight

Design BEFORE implementation — 2026-10-26 Asia/Taipei. Acceptance pending actual tests.
Saved dirty HEAD/status/SHA and all 199 original source files. HEAD
50d6d77bbb21df721f28486474689e77e7bce71e. No reset/clean/staging/SCM action.

## Inspection / security findings
M1 HTTP and worker currently use main.create_app and injectable fake adapter bundle.
M2 modules enforce Principal/Run/grants/fencing and durable intents, but standalone
low-level interfaces do not require resource quota; M2.4.4 wrapper is optional.
The deployed Docker CMD still targets the M1 factory. CI builds only Sandbox/Reader,
not QA and pass/fail/zero Review QA images; this permits Linux tests to skip. M1
cancel releases its monetary reservation by default, which is valid for fake work
but must not be reused to declare unknown external operational costs settled.

## ADR 0015 / selected architecture
Add entrypoints/{contracts,boundary,gateway,bootstrap}.py and __init__. Formal
bootstrap has explicit disabled/fake/operational modes. Disabled is fail closed;
fake mode validates exact known side-effect-free adapters at dispatch; operational
mode requires a trusted preconstructed FormalExecutionGateway. No dynamic imports,
Agent host paths/commands/Docker flags or implicit fallback to fake.

Keep main.create_app and unbound low-level adapters compatible for the existing M1
tests/trusted embedding. They are compatibility construction interfaces, not approved
operational deployment entrypoints. Default no-engine M1 construction is fake-only;
formal bootstrap always enforces strict fake validation even with injected engine.
All supported deployment/dev commands point to formal bootstrap. Trusted host code
can always construct raw adapters; Python context guards are NOT an OS sandbox.
No Agent is allowed to execute Python in the trusted controller or choose factories.

Formal execute uses the existing authenticated HTTP execute route and fixed trusted
profile. Before effects: Run-first lock/current grants/policy, expected version,
issued generation, original lease, M1 monetary reservation and aggregate counters;
then persist a versioned FormalDispatch intent in IdempotencyRecord/WorkflowEvent.
Use existing quota manager/controller to reserve resources and commit IN_USE before
calling verification. Scope permits are local opaque nonserializable capabilities,
bound to this graph/actor/Run/generation/dispatch/reservation and invalidated on exit.

Bind guards only to the formal graph's public resource/execute/publication methods,
generic Docker and Git dispatch, private Git construction and LocalObjects IO.
Direct calls on this bound graph without a formal scope fail before effects. The
existing shared Workspace authorization hook additionally validates durable formal
intent, held quota and monetary reservation in the SAME already Run-locked DB
session, avoiding nested-transaction deadlocks. Driver commands need a trusted
semantic adapter boundary, not merely an active scope; push/merge/prune are forbidden.
Legacy unbound objects retain compatibility and all prior tests.

OperationalPrototypeController gains an optional trusted execution_scope callback,
default no-op. Only formal composition installs it. Admission/control permits cannot
create workloads/publish; execution permits are issued only after durable IN_USE.
Expired/revoked leases and changed generations/grants fail closed. A DISPATCHING/
UNKNOWN intent never dispatches again. Completed cached receipts require current
ownership/credential/grant and exact original request identity; no new byte rights.

Owner-only formal administration wraps initialization, read/health/review, Holds,
quota release and retention plan/approval/execution. Cleanup uses durable GC approval
and the existing per-target Run-lock guard, never raw Reconciler bypass. Emergency
stop requires the existing persisted revoked-lease stop intent and permits stop only,
not create/publish/delete. HTTP operational cancel preserves unknown monetary and
resource commitments; stop is best effort and reported as confirmed/failed, not
inferred resource absence. Quota release still requires fresh exact absence. Human
merge rules and unverified Artifact assessments remain unchanged.

Formal workload recovery execution is not exposed as a new tool in this milestone;
bound publication methods require the execution gate and cannot be called directly
through legacy Recovery. Existing unbound M2.4.3 APIs remain available to trusted
diagnostics/tests. Unknown formal intents require reviewed intervention; no automatic
takeover/replay or fabricated completion/cost receipt is introduced.

FormalDispatch v1 reuses existing JSON/audit tables; no SQL DDL, head0007. Safe request
keys hash the original HTTP key; journal binds expected version/profile/config SHA/
owner/actor/Run/generation and explicit DISPATCHING/COMPLETE/BLOCKED/UNKNOWN states.
Result uncertainty never auto-releases resource commitments. Configuration, image and
store identity are trusted operator wiring, never a generated file authorization.

## CI / operational gate plan
Update the repository workflow to build/pin Sandbox, Reader, QA and three Review QA
variants; validate all immutable IDs; run the entire Linux suite with explicit ENV,
raw logs/JUnit and a zero-skips assertion. Pin already locally used pytest-xdist/
execnet versions for bounded CI parallelism. No remote run/push claim. Inspect Git
remote/author/upstream status without printing credentials or modifying refs.

The existing 1 stopped container + 3 volumes remain separate OPEN/QUARANTINE manual
items, never fixtures, mounted/adopted or auto-deleted. Token/usage/cost work is design
and gap analysis only: provider-authoritative receipts vs estimates vs unknown,
stable model/version/price references, immutable correlation and reconciliation;
no real Codex/provider billing integration or invented prices/actuals.

## Planned scoped files
New entrypoints package, formal-dispatch.v1 schema, docs/27-FORMAL-ENTRYPOINTS.md,
ADR0015, new test_formal*.py contracts/integration. Minimal edits main.py (dispatch/
cancel hooks), service.cancel optional preserve-budget default false, operations
controller optional scope callback, Docker CMD/dev command, CI/requirements and README
append. Preserve all existing tests, models, migrations and merge logic.

## Test plan (contracts before implementation)
1. Formal bootstrap disabled/fake/operational: no implicit runtime; strict fake bundle
   rejects injected/monkeypatched non-fake effects before dispatch; raw compatibility
   tests retain their behavior. Normal authenticated HTTP operational execution works.
2. All bound low-level resource/driver/filesystem bypass attempts denied; outside scope,
   wrong actor/Run/profile, control/admission/inspect scope, generic effect dispatch,
   push/prune and stale escaped scope. No test runs untrusted host code.
3. Quota exhaustion, missing/corrupt monetary reservation/counters, revoked policy/grant,
   expired/revoked lease/generation, Human Cancel, DB/Docker failure. No external effect
   before durable authorization/quota intent; UNKNOWN/cancel retains commitments.
4. Concurrent admission/same/different keys, restart/cache, dispatch/final commit faults,
   no unknown replay. Artifact/Review Bundle SHA and assessment remain intact.
5. Formal owner cleanup approval/Hold/quarantine and quota release guards; emergency
   stop only from persisted revocation, no premature deletion or capacity release.
6. Real Linux Docker HTTP formal E2E and rejection/cancellation; SQLite/PostgreSQL;
   separate Windows skips. Full 1364 original unique IDs plus new cases in one complete
   invocation per platform; migration/drift/Ruff, frozen source/image provenance.
7. Static workflow configuration tests plus local CI-equivalent full Linux invocation;
   raw logs/JUnit and zero-skips check. Remote CI remains UNVERIFIED until actually run.

Deliver entrypoint inventory/matrix before/after with source anchors; architecture/
ADR/test plan/source/schema/diff; all actual evidence/failures; manual quarantine list,
remote CI and token/cost gaps; M3 matrix/acceptance with unresolved gates explicit.
No Codex/M3–M5/commit/push/merge/deploy or production environment change.

## Fault-driven integration amendment

A formal QA transport fault returns a persisted UNKNOWN verdict. Fault injection found that legacy Verification unconditionally settled the monetary reservation as deterministic zero cost, even for UNKNOWN. Add a default-false preserve_budget constructor option around both existing settlement sites; formal Gateway always enables it and binds/checks it in the configuration. Legacy unbound deterministic regression keeps its original behavior. Formal success/failure/UNKNOWN and cancellation retain monetary commitments until separate adequate settlement evidence; resource capacity also remains held until exact absence proof. New regression asserts UNKNOWN, preserved money/capacity and cached non-replay. The initial failing raw evidence is retained. This is a minimal required formal integration fix, not a billing implementation or human merge rule change.
