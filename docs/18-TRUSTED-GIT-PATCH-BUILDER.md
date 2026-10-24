# Trusted Git Patch Builder — M2.3.3

Internal API: `TrustedGitPatchBuilder.build(actor, run_id, import_id, lease_token)`.
The trusted service constructs `LocalGitPatchBuilder(importer, private_root)`; callers
cannot supply paths, commits, commands, modes, patches or Git options. No HTTP endpoint
or Agent tool is added, and no M1 runtime adapter is replaced.

The importer must have committed IMPORTED. The builder rechecks active original worker
lease, owner, Workspace/Sandbox, repository identity, grant intersection, base commit,
manifest key/UUID/hash/proof and all raw/file artifact bindings. It strictly validates the
v1 manifest and archive again. Every file must match the archive and ArtifactStore SHA-256.
Source artifacts are reread after construction and publication to catch tampering.

```python
builder = LocalGitPatchBuilder(importer, operator_private_root)
result = builder.build(actor, run_id, import_id, lease_token)
assert result.assessment == "unverified"
# result.applicability is "applicable" or "empty_no_changes"; never a QA result.
```

A new private bare repository contains a complete clean base index and only necessary
verified authorized blobs. Tree object ID must equal the pinned commit's root tree;
unchanged out-of-scope blob contents are not copied or read. Output files are reconstructed
as data in a separate private directory and staged via raw hash-object/update-index stdin.
No source worktree/ref/index/ODB writes; no checkout, Git add/filter, commit, fetch or code.
A second private base index performs real `git apply --cached --check` and apply. The full
resulting index must match base plus explicit output files, and every output blob must match
source bytes/SHA-256. This checks applicability to a clean base INDEX, not a full checkout
or execution environment, and does not run project tests or QA.

MVP is add/modify only. Missing files stay unchanged. Same content under a new name is an
addition, not a rename. Preserve existing 100644/100755 modes; new files are 100644. Output
archive permissions do not transfer through the content-only import contract; extra mode,
delete or rename fields are rejected. Links, special files, submodules, unsafe/colliding
paths, unsupported tree/index states and any unexpected changed path fail closed.

Use a private Linux service-owned root (Windows portable development checks only), outside
repo/workspace/artifact/capsule roots, never mounted into workloads. Git uses fixed argv,
shell=False, isolated env/config, empty templates/hooks, no network/pager/filters/textconv/
external diff or rename detection. Git output uses private temporary files; patch reads
are capped at 16 MiB and commands at 30s. Needed base blobs are size-checked BEFORE content reads and capped at 8 MiB each /64 MiB
total, raw base commit at 1 MiB, base index at 10,000 paths; inherited output caps remain 256 files/4 MiB each/8 MiB.
Temporary Git output disk use is bounded by inputs/time, not a filesystem quota. Global
service retention/concurrency quotas are not implemented here.

The existing M1 IdempotencyRecord is a versioned namespaced build journal:
`patch-builder.v1:<import UUID>`, unique owner/principal/key. No new SQL model/migration.
The strict schema is `contracts/patch-build.v1.schema.json`. Journal input fingerprint
includes builder/Git versions and source bindings. Collision or changed request is denied.
States: PENDING -> BUILDING -> BUILT -> PUBLISHING -> READY, or FAILED. WorkflowEvent source
is `trusted_git_patch_builder`. Audit contains stable codes/IDs, not tokens or raw content.

Pure BUILDING computation can be deterministically retried from the same immutable inputs.
Only a byte-identical complete candidate is adoptable; partial/changed candidates are
terminally rejected. BUILT/PUBLISHING retry reads candidate/proof integrity and uses fixed
ArtifactStore keys. Artifact publication is outside the journal write lock (SQLite safety).
No completed result before both patch/proof artifacts and final READY transaction commit.
Partial artifacts may remain unverified on failure. An underlying ArtifactStore key that
has become terminal FAILED/CORRUPT is not overwritten or automatically reset; the same
build cannot recover that key. Retry guarantees apply only to safe complete candidates
and recoverable/nonterminal publication outcomes. DB/audit outage is explicitly reported.

Patch and BuildProof artifacts retain Evidence v1 unverified/source classification.
Historical owner reads use existing ArtifactStore authorization. Building/replaying requires
an active original lease and READY Workspace; expired or revoked runs cannot rebuild.
No M2.3.4 QA runner, M2.3.5 verification, M2.4 reconciler, real Codex or merge/deploy exists.

Scratch directories are freshly allocated under the private root; cleanup verifies the
resolved boundary and never follows an arbitrary caller root. Crash/orphan/retention cleanup
remains an operator concern. Hostile same-UID/admin/daemon access is outside the trust model.
Failure injection is not actual power-loss or multi-process stress evidence. See ADR 0008
and the separate acceptance report for exact executed tests and unverified limitations.
