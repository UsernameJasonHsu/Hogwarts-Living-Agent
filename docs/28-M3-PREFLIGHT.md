# M2.5 operational preflight boundaries

This is a local deterministic Controller milestone. M3 is NOT READY until the
separate release gates have evidence. No provider runtime or M3 tool is introduced.

## Approved entrypoint and operator configuration

Use `hogwarts.entrypoints.bootstrap:create_formal_app`. Default execution is disabled;
fake is explicit and validates the exact pure fake implementation. Operational mode
requires a trusted preconstructed `FormalExecutionGateway`, one owner, all six private
storage roots, approved runtime/QA profiles, immutable image IDs, current repository
grants and initialized quota. Environment selection alone grants no execution right.
The compatibility `main.create_app` injected-engine API and unbound adapters remain
trusted Python construction interfaces. They must not be exposed as Agent tools.
Python scope guards are not a hostile-process security boundary or Linux sandbox.

## Uncertainty and manual intervention

Persisted DISPATCHING/UNKNOWN means do not re-dispatch, including after restart.
Duplicates cannot invalidate a concurrent winner. Do not erase an intent to retry.
Correlate Run, original generation, dispatch, quota receipt, workload resources,
artifacts and audit history. Use existing recovery diagnostics and a reviewed human
decision; this milestone exposes no automatic formal workload recovery executor.
Cancel records the existing revocation and best-effort stop, retains monetary and
resource commitments, and reports stop state. A confirmed stop is not deletion or
cost settlement. Reviewed retention cleanup must re-inspect exact identity/references;
quota release independently proves absence. Never replay UNKNOWN or fabricate cost.

## Release gates

| Gate | Required evidence | Current scope |
|---|---|---|
| Formal admission | Current Principal/Run/grant/fence, durable intent, monetary and resource commitments | Implemented for approved graph; local results in acceptance report |
| CI | All six pinned image variants, full Linux suite, raw logs, zero skips | Configuration updated; remote execution UNVERIFIED |
| Git release | Reviewed dirty diff, correct upstream, authorized manual commit/push | No automatic SCM action; upstream/auth checks separately reported |
| Quarantine | Human disposition of one stopped container and three volumes | OPEN; no deletion, mount, adoption or fixture use |
| Storage | Physical disk/volume limits, pressure behavior, filesystem accounting | Logical admission only; native hard quota remains a gap |
| Production operations | Native Linux deployment, load, backup/restore, daemon restart and alert integration | UNVERIFIED; local Docker Desktop is not production evidence |
| Model usage/cost | Authoritative provider receipts, versioned pricing, partial/unknown reconciliation | Design only; no Codex/provider integration |

## Token, usage and cost ledger design (not implemented)

Store immutable provider request/attempt IDs correlated with Run, dispatch, generation,
profile/model/version, time and source. Separate admission estimates, monetary
reservations, actual authoritative usage, partial and unknown receipts. Preserve input,
cached-input/output and any provider-specific billed categories without assuming a
fixed universal token vocabulary. A versioned price reference, currency and explicit
integer rounding policy are required before calculating actual cost. Deduplicate
provider receipts and attempts, record adjustments in append-only reconciliation,
and never bill a retried unknown attempt twice or treat reservation as spend.

Missing gates include real provider authentication/SDK/runtime, receipt validation,
price governance, delayed billing reconciliation, aggregate monetary ledger migration,
retention/privacy and operator reporting. Do not store prompts, host credentials or
secret tokens as usage evidence. No invented model prices or verified receipts.

The delivered M2.5 raw evidence, entrypoint matrix, manual quarantine list and acceptance
report determine local acceptance. They do not authorize M3, merge or deployment.
