# Current status / historical record — M2-RC1

Current implementation covers M1 and M2.1–M2.5, with RC1 security/CI remediation.
Alembic head is0007. M3 is NOT READY; real Codex and production deployment are absent.
Current required cases/source identity are versioned in validation/required-cases.v1.json;
actual RC1 results belong to the separately delivered raw validation report.
The last completed M2.5 baseline was Linux1471/0/0 and Windows1309/0/162.
Those counts are historical, not a claim that modified RC1 bytes already passed.
Remote CI remains UNVERIFIED. All Artifact assessments remain unverified.
See [RC1 release gates](29-RC1-RELEASE-GATES.md).

The original dated record below is retained verbatim. Its831-case totals, head0006,
and per-milestone exclusions describe that earlier snapshot, not current status.

---

# M2 Implementation and Verification Status

Recorded 2026-10-24. This consolidates the existing local implementation and
historical test evidence for version control. It does not add runtime features.

## Implemented scope and commit order

| Milestone | Implementation | Design |
| --- | --- | --- |
| M2.1 | Git Workspace Manager; pinned base, grants, isolated worktrees, diff, lifecycle | [ADR 0004](adr/0004-m2-1-git-workspaces.md) |
| M2.2 | Linux-only secure sandbox; approved profiles, resource limits, lifecycle | [ADR 0005](adr/0005-m2-2-secure-sandbox.md) |
| M2.3.1 | Versioned evidence, immutable local ArtifactStore, SHA-256 and metadata | [ADR 0006](adr/0006-m2-3-1-evidence-artifacts.md) |
| M2.3.2 | Authenticated stopped-output snapshots, bounded secure import | [ADR 0007](adr/0007-m2-3-2-secure-output-import.md) |
| M2.3.3 | Authorized add/modify-only text/binary patch and independent apply check | [ADR 0008](adr/0008-m2-3-3-trusted-git-patch-builder.md) |
| M2.3.4 | Approved QA profiles, new Linux QA containers, independent execution attestation | [ADR 0009](adr/0009-m2-3-4-trusted-qa-runner.md) |

M1 governance, Fake Adapters and earlier regression tests are retained. SQL
migrations 0003, 0004, 0005 and 0006 accompany their respective milestones.
M2.3.3 and M2.3.4 reuse the existing idempotency journal; migration head is 0006.
All Imported/Patch/BuildProof/QA Artifact assessments remain **unverified**.
Patch applicability and QA execution results do not promote Agent reports.

## Actual prior test evidence

| Invocation | Passed | Failed/errors | Skipped |
| --- | ---: | ---: | ---: |
| Complete Linux suite, one invocation | 831 | 0 | 0 |
| Complete native Windows suite, one invocation | 747 | 0 | 84 |
| Final Linux QA focused suite | 130 | 0 | 0 |
| Final Windows QA contracts | 14 | 0 | 0 |

These are separate invocations, not additive counts. Both full runs contain all
701 prior regression case IDs. Linux validation used a trusted Linux Controller
container with the Docker Desktop Linux daemon; a separate native Linux server
was not validated. Windows is a development host, not a sandbox runtime.

The Linux run includes SQLite/PostgreSQL migration roundtrip and drift checks.
Ruff check and format passed in the previous acceptance run. Actual target QA
outcomes include 6 pass, 6 fail, 4 timeout, 2 infrastructure_error and 2
unknown_execution; expected-negative harness cases passing do not change target
failures into successful QA results.

Windows started before the final Linux-only QADockerDriver._kill_job correction.
All portable modules/tests/schemas matched; final Linux focused and full runs
validated that correction. Windows Linux-runtime cases were skipped, and the
final Windows contracts were rerun. No composite full-suite count is asserted.

Before commit organization, all 120 nonignored source files matched the saved
final source manifest byte-for-byte. Organization changes only README and this
status document. Intermediate milestone commits have not been individually
retested; the full evidence applies to the completed implementation tree.

Actual commands used the approved Python 3.12 harness and dedicated test DB:

```text
python -m ruff check . --no-cache
python -m ruff format --check . --no-cache
python -m pytest -x -v --junitxml=<evidence>/linux-full-junit.xml
python -X utf8 -m pytest -x -v --junitxml=<evidence>/windows-full-junit.xml
python -X utf8 -m pytest tests/test_qa_contracts.py -q --junitxml=<evidence>/windows-contracts-final-junit.xml
```

Linux commands ran inside the trusted test Controller with exact approved sandbox,
reader and QA image IDs; native Windows commands used the dedicated Python
dependency path. Credentials, generated private state and raw runtime outputs
are excluded from Git. The local M2.3.4 acceptance bundle retains the JUnit files,
raw logs, complete harness commands and failure history. JUnit fingerprints:

| Evidence | SHA-256 |
| --- | --- |
| `linux-full-junit.xml` | `6d3e1a7b48e320e83790c95ea5c38bf2352513fe0656eb64ae2a7262f8eb2666` |
| `windows-full-junit.xml` | `4f4c82f7e28bc0c40777fc729ab5143ad9a69c1cc8925ed20d871c8b6b2af084` |
| `linux-focused-junit.xml` | `6bf492aeb907ddb6dc594b5f6c947e8747acf55cff3f54c73b5744d0208bd0a0` |
| `windows-contracts-final-junit.xml` | `f40b777b5e7193c45b1e7072009dda9ea480ab87ffd7bec092ac3b99be5456c6` |

## Unverified items and limits

- Remote GitHub Actions is unverified until a specific run is inspected.
- Existing CI builds sandbox and reader images, but does not build/pin
  HOGWARTS_QA_IMAGE; real Linux QA tests can skip there. A green run alone
  therefore does not establish complete M2.3.4 runtime coverage.
- No real Codex runtime, M2.3.5, M2.4 reconciler, automatic merge or deployment.
- Worktrees are not security sandboxes. Approved QA images and trusted Controller
  remain part of the trust boundary; no hostile-host or kernel-escape claim.
- QA exit status does not establish test coverage or application correctness.
  Unknown execution is not automatically replayed.

M2.3.4 is accepted only in the documented local scope. Remote CI and broader
production readiness are not marked passed.
