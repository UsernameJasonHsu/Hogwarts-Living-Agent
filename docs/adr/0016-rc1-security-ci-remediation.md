# M2-RC1 remediation architecture and test plan

Design before implementation,2026-10-26. Saved original216 byte fingerprints and dirty
status, HEAD50d6d77bbb21df721f28486474689e77e7bce71e. No reset/clean/stage/SCM/deploy.

## RC-FIX-01

Versioned validation/required-cases.v1.json records exact JUnit(classname,name) IDs,
explicit Windows skip IDs/reasons, Linux zero skips and a source-file hash map.
Source hashing uses documented UTF8/LF normalization for cross-platform Git checkout;
raw-byte snapshots remain separate. No whitespace/content normalization beyond CRLF.
Manifest excludes itself from its source map to avoid recursion. Release identity
binds BOTH source digest and full required-manifest SHA. Commit allowlist contains
paths, not recursively self-referential hashes, and is part of hashed source.

An operator-reviewed manifest is generated BEFORE tests, never derived from the
JUnit being accepted in CI. Trusted launcher checks source/collection before and
source again after full pytest, records original JUnit SHA/source/version/manifest/
collection/command/platform/images and optional actual GitHub commit/run metadata.
Gate rejects missing/extra/duplicate cases, failure/error, nonapproved skip/reason,
wrong source/release/manifest/JUnit hash, incomplete launcher provenance and selectors.
No claim against malicious same-process CI/operator code; repository review and CI
runner are trusted boundaries. Remote CI remains unverified until actually run.

Preserve versioned historical M2.5 case manifest from ORIGINAL1471/1309+162 JUnit
and216-source evidence. Offline historical case/source validation is explicit;
it is not fabricated CI provenance. Reproduce original full case-set acceptance
and rejection after removing107 M2.5 IDs. Current RC1 manifest includes all old IDs
and new remediation tests. No minimum-count-only fix or automatic skip approval.

## RC-FIX-02

Production worker requires out-of-band trusted deployment configuration: origin,
deployment ID, immutable Controller image pin, release identity, approved Gateway
profile fingerprint and a separate >=32-byte deployment signing key. Never learn
trust from health/first response; no fake/legacy fallback. TLS for non-loopback
origins, no proxy/redirect trust, canonical IDs and configuration validation.

Canonical operational factory requires deployment identity. It registers a dedicated
formal-worker prefix bound by closure to the exact FormalExecutionGateway. Legacy,
disabled and fake factories expose NO such operational routes. Signed challenge,
per-request method/path/body/time/nonce binding and signed response with the same
nonce/descriptor prevent plain health spoofing, wrong config and receipt substitution.
No deployment secret enters Agent requests/artifacts/logs; key repr is hidden and
domain-separated HMAC/timing-safe compare is used. Image identity is an operator
pin, verified by the trusted deployment harness; not hardware/container attestation.

Context/claim use EXISTING M1 ownership/service.command/claim; execute calls EXISTING
Gateway. Signatures are additional deployment binding, not Policy/Lease/Quota/Budget
authorization. All old gates, UNKNOWN commitments and unverified Artifact semantics
remain unchanged. Official worker only uses the dedicated prefix, so a legacy
injected server cannot receive a standard execute by mistaken fallback.

Fake mode has a distinct in-process canonical-fake test helper, never a production
CLI mode or generic remote legacy client. Trusted host Python/raw adapters remain
compatible and are not an OS sandbox. Wrong origin/key/mode/release/image/profile,
missing trust, forged/replayed proof or absent canonical routes fails closed.

## RC-FIX-03

Harden ignore rules for env variants/private keys/private state/test evidence; avoid
patterns that hide product outputs/artifacts packages. Docker context uses default
deny plus approved source roots and final sensitive/cache exclusions. Versioned
commit path allowlist plus offline redacted source/secret/context audit runs before
CI image builds. No raw match values or host credential-store inspection.

Use actual Docker COPY/export canary test on a disposable source copy to confirm
env/key/private/test artifacts excluded; no unknown resource/volume use. README and
docs20 clearly separate current RC1/M2 state from historical counts/head0006.
ADR0015 original record retained byte-for-byte as historical body with a status
clarification; new ADR0016 records the remediation decision. No historical decision
rewritten, no M3/runtime/model integration, no SQL DDL; Alembic remains0007.

## Affected files / regressions

New release manifest/audit/collection/runner helpers and versioned validation files;
deployment protocol + canonical worker routes; production worker and separate fake
test helper; bootstrap integration; existing formal tests updated only to supply
explicit trusted deployment config. CI workflow, .gitignore/.dockerignore, README,
docs20/ADR0015 clarification, new ADR0016 and operator runbook. Preserve all1471 old
case IDs/assertions; fixture configuration changes do not waive guards.

Tests before implementation: manifest full/subset/missing/duplicate/extra/failed/error/
skip/reason/platform/source/JUnit/provenance tampering; request/response HMAC and TTL/
nonce/origin/config tests; legacy Marker denied before effects; canonical operational
claim/execute plus real Linux Docker smoke; explicit fake test helper isolation;
ignore/allowlist/secret negative cases and actual Docker context canaries.

Run focused tests, Ruff, then freeze reviewed source/manifest and run ONE complete
Linux/SQLite/PostgreSQL regression and ONE complete Windows regression with raw
JUnit/logs. Exact old1471-ID subset and new expected set must match. Record all
failures; do not relax original Sandbox lifetime/isolation. Migration/head and
cleanup evidence separately. No retry of UNKNOWN workload; each test fixture new.

Four pre-existing QUARANTINE resources are not operated on, mounted, cleaned or used
as fixtures. Dedicated known test DB/network/containers may be created and precisely
cleaned after validation; no broad prune. Remaining remoteCI/quarantine/startup-risk
release blockers stay open regardless of local fixes. RC status derives from actual
evidence, not this design or generated manifests alone.

## Design refinement: explicit test separation

Production deployment validation also requires exact Linux OutputDockerDriver and QADockerDriver types. Fake-backed Gateway unit regression uses a tests-only in-process service facade with no formal-worker routes; it cannot be mistaken for a production Worker endpoint. Generic service regression assertions remain unchanged. Canonical operational missing deployment identity fails startup, and real Linux signed-worker smoke covers the production path. Sandbox/Reader subcontexts get explicit-file .dockerignore policies; QA builds from repository context, matching its COPY paths.

The original M1 one-shot Worker test keeps its Test IDs and review/evidence assertions, but now explicitly uses canonical fake app + in-process fake_worker. Calling production worker against a legacy fake server is intentionally rejected by RC1. The initial full run exposed this compatibility contract and is retained as incomplete/failed evidence, never a passing full suite.

The initial real Linux Smoke retains five failures: two signed-worker fixtures sent an unsigned default Idempotency header (correctly denied), and three context probes lacked Buildx in the local Controller. The fixture now mirrors production's Authorization-only client defaults; action keys remain signed explicitly. CI provisions/checks Buildx and the trusted local harness installs its pinned plugin. Neither failure is waived or converted into a pass.

## Complete-regression findings and minimal corrections

The first complete r1 runs retain Linux1576 pass/1 fail and Windows1400 pass/2 fail/175 skip. Linux GC correctly blocked retention_resources_changed. Four deterministic DB-order contracts prove that identical journal/import rows in reversed return order changed inventory identity. Canonical sorting now stabilizes BOTH binding and recovery-evidence fingerprints; actual content mutations remain identity changes. No old journal, evidence, approval, fencing or deletion rule is rewritten or relaxed. An old plan whose observed hash differs still fails closed and needs a fresh reviewed plan.

Windows failures exceeded the unchanged five-minute lease (309/313 seconds); the escaped-context assertion was not reached. Subsequent complete platform runs are scheduled separately to validate the load effect, rather than extending leases, bypassing heartbeat/fencing or approving new skips. No UNKNOWN Run is replayed; each regression uses new disposable fixtures.

A COPY-only synthetic counterexample also showed that a generic unlisted JSON under an allowed source directory entered Docker context. Root .dockerignore now negates exact reviewed files only; no directory wildcards. A minimal engine probe confirmed nested exact paths work without broad parent includes. Nine private/unlisted canaries cover this, and approved child contexts remain explicit. Required-case suite revision r2 retains every previous1577 ID and adds deterministic regression contracts.
