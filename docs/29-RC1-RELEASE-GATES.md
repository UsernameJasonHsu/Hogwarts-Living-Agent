# M2-RC1 release gates and operational trust

M1/M2.1–M2.5 implementation is present. M3 remains NOT READY. No remote CI success
is established by local evidence. Historical milestone counts are retained in docs20
and ADR0015; ADR0016 records this subsequent decision. Current required cases are
versioned in validation/required-cases.v1.json and commit paths in
validation/commit-allowlist.v1.json. No new schema migration; head remains0007.

## Reviewed case and source policy

The trusted maintainer updates the manifest after reviewing source and collected
IDs; CI never derives its policy from submitted test results. The current policy
retains every1471 pre-RC1 ID. Linux permits no skips. Windows permits only the
reviewed exact IDs and skip reasons, including Linux-only integration cases.
Counts alone never authorize release. Extra, duplicate, missing, nonpassing and
unapproved skipped results fail. All six workload image IDs must be SHA256 pinned.

Run `python scripts/release_audit.py --output test-results/release-audit.json`,
`python scripts/ci_runner.py --workers 4 --output test-results`, then
`python scripts/ci_gate.py --junit test-results/junit.xml`.
The runner has no subset option. TEST_DATABASE_URL is mandatory; PYTEST_ADDOPTS
must be empty. Six image variables are listed in scripts/ci_gate.py. It records
precollection IDs, full original JUnit/raw log hashes, source/manifest/release
identity and actual exit code, then rechecks source and the original JUnit.
Raw bytes remain unmodified. Source hashing normalizes CRLF to LF only for Git
cross-platform checkout equivalence. Manifest bytes have their own immutable hash;
combined release SHA binds both source digest and manifest bytes. The historical
manifest cannot authorize a current release. Provenance is trusted CI output, not
a cryptographically independent attestation of an untrusted host or LLM report.
Archive the entire test-results directory even when validation fails. Docker context
probes require the Buildx/BuildKit CLI plugin; CI explicitly provisions and checks it.

## Worker deployment binding

Production worker.py requires DeploymentTrust provisioned by a trusted operator:
HOGWARTS_WORKER_ORIGIN, HOGWARTS_DEPLOYMENT_ID, HOGWARTS_CONTROLLER_IMAGE_ID,
HOGWARTS_RELEASE_SHA256, HOGWARTS_GATEWAY_SHA256, HOGWARTS_DEPLOYMENT_KEY.
The HMAC key is separate from human/worker API tokens and must never enter Agent,
sandbox, artifacts or build context. Provision it through the trusted controller's
secret channel. Do not copy values from a server health response. TLS is required
except explicit loopback development. No redirect, proxy-env trust or legacy/fake
fallback. A nonce/timestamp/body/path/method/idempotency-bound challenge and each
signed response must match out-of-band pins before the Worker proceeds.

The operator constructs exact DeploymentIdentity and passes it to
create_formal_app(engine, mode="operational", gateway=..., deployment=...).
It validates exact trusted Gateway, real Linux drivers, preserve-budget setting,
source identity, profile/roots/controller bindings before installing dedicated
formal-worker routes. Missing bindings fail closed. Default Compose is disabled;
it does not silently construct an operational Gateway from environment strings.
Operator image ID is an out-of-band assertion, not hardware/process attestation;
local container evidence separately records the actual inspected controller ID.

The Gateway retains Principal/Policy/Run/Lease/Fencing/Quota/Budget/Audit controls.
The deployment signature grants no new M1 authority. Replayed action requests use
existing durable idempotency; an uncertain execution is not retried by the Worker.
The fake_worker.py helper requires an in-process canonical fake TestClient;
there is no remote fake CLI. Tests' inprocess_service facade has no production
worker routes. Old M1 create_app remains for compatibility, but the formal Worker
never calls its legacy execute endpoint. Arbitrary trusted host Python injection
remains outside this deployment boundary; low-level adapters must not be exposed
to Agent code.

## Commit/context hygiene

Review only exact commit-allowlist paths; never `git add .` blindly. The audit
rejects additional eligible files, private paths, links, nontext/oversize source,
credential patterns and ignore/context drift. Root Docker context defaults deny and permits only exact commit-allowlist files;
Sandbox/Reader subcontexts allow only explicit files. QA uses repository-root
context matching its COPY paths. Tests copy synthetic canaries with FROM scratch
only, never real host credentials. Pattern scanning does not prove absence of
all secrets; trusted review remains required. Ignored/private contents are neither
published nor added to the source manifest. All artifacts remain unverified.

## Outstanding release decisions

Remote CI requires an authorized commit/push and actual new run; this task does
neither. The four previously quarantined unknown resources remain human-owned
review items and are never remediation fixtures. The prior intermittent Docker
startup failure retains its original evidence and unproven root cause. Release
owner must adjudicate those operational blockers. No merge/deploy or M3 readiness
is implied by a local RC-FIX pass.
