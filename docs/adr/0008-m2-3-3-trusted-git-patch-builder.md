# ADR 0008 — M2.3.3 Trusted Git Patch Builder

Status: architecture and test contract BEFORE implementation, 2026-10-24.

## Boundary and interfaces

Preserve branch/HEAD and all 569 existing tests and previous M1–M2.3.2 implementations.
Add internal TrustedGitPatchBuilder Protocol and LocalGitPatchBuilder, constructed only
by trusted operator code from the existing LocalOutputImporter and a separate private
root. build(Principal, run_id, import_id, lease_token) accepts no host path, patch bytes,
base override, Git arguments or shell string. No new HTTP endpoint or Agent tool.

Reuse M1 Principal/Run/current worker lease, Workspace/RepositoryGrant checks, M2.3.2
import binding and M2.3.1 ArtifactStore. Require IMPORTED, matching owner/producer/original
lease, Run/Workspace/Sandbox, unchanged repository/proposal/config and pinned Base SHA.
Validate source Manifest artifact id/hash/kind/bindings and deterministic importer keys;
strictly parse its v1 schema with no extra fields. Compare its contents to OutputImport's
committed file/proof records. Read raw archive and every file through ArtifactStore,
revalidate USTAR paths/types/limits/current output prefix/grants, compare exact file sets,
SHA-256, sizes and bytes. Treat imported content as untrusted data, always unverified.

## Trusted temporary Git construction

Create a new private bare Git repository plus data-only work directory and two private
indexes. No linked source worktree, clone/fetch, source ref/index/ODB writes, checkout,
Git add/filter, commits, merge, source commands, dependency installation or QA execution.
Use absolute approved Git, fixed argv, shell=False, isolated minimal config/environment,
empty hooks/templates, disabled protocols/attributes/external diff/textconv/renames.

Validate the authorized source's full 40-hex commit and ordinary tree. Verify raw commit
object hash and its root tree. Build a complete base index from validated path/mode/blob
records, writing tree objects with write-tree --missing-ok; the root must exactly equal
that commit's tree. Only referenced authorized base blobs needed for patch targets are
read into the private ODB, with Git object hash/size validation. Unchanged out-of-scope
blob contents are NOT copied. Thus clean-base validation is a complete base INDEX plus
only necessary authorized blobs, not a full filesystem checkout of unrelated files.

Reconstruct only explicit output regular files under the private work directory using
exclusive/no-follow writes and bounded safe FD reads. Preserve existing tracked regular
modes (100644/100755), new files 100644; no mode request is accepted. Reject symlink,
hardlink/reparse/special files, file-directory/case collisions, submodule and unsafe trees.
An omitted file remains unchanged; identical content at a new path is an addition, not
a rename. No deletion/rename inference and no permission/attribute/filter transfer.

Stage raw verified bytes via hash-object --stdin and update-index --index-info. Never
run Git on an Agent-supplied pathname or use implicit add-all. Export binary/full-index
patch with explicit deterministic diff options. Validate all name-status paths and
A/M statuses against predicted changes and all current grants. Recheck private file
identities, hashes and exact file set after export; reject TOCTOU.

Initialize a second clean index from the exact Base tree. Execute real Git apply
--cached --check, then apply to that private index. Verify the ENTIRE resulting index
matches the expected base+explicit outputs, modes unchanged, and cat-file bytes for
outputs exactly match source SHA-256. Empty/no-change output yields empty patch and
explicit empty_no_changes applicability; it does not infer deletions or claim a nonempty
apply test ran. Apply checks prove applicability/integrity only, NEVER QA Passed.

Limits: inherited output bounds (256 files/4 MiB each/8 MiB total); base 10,000 paths,
8 MiB per needed blob/64 MiB total needed base bytes; patch/artifact <=16 MiB; Git command
timeouts 30s. Capture Git stdout/stderr through private temporary files, bounded reads;
Git disk output is bounded by these input limits and timeout, not an OS filesystem quota.

## Durable state and retries

Reuse existing IdempotencyRecord as a namespaced versioned build journal (key
patch-builder.v1:<import UUID>, unique owner/principal/key); typed response stores immutable
input fingerprint, current state/version, patch SHA, proof hash/result and artifact IDs.
No new DB table/migration is needed. This retains existing M1 and migration behavior.
Namespace collision or fingerprint disagreement fails closed; a prompt cannot grant
journal authority. States PENDING -> BUILDING -> BUILT -> PUBLISHING -> READY, or FAILED.
WorkflowEvent source=trusted_git_patch_builder records intent/transitions/denials, no
secrets/tokens/raw stderr/content. DB+audit outage returns unavailable honestly.

Commit intent before filesystem work. SQL row write lock serializes pure computation.
Artifact reads/publication occur outside that write lock to avoid nested SQLite writers.
Private immutable candidate file uses journal UUID, exclusive/fsync/SHA checks. Exact
BUILDING retry can recompute the deterministic result from immutable validated inputs
and adopt only identical complete candidate bytes. A partial/different candidate fails
closed; no blind overwrite. Git version and builder version are request fingerprinted.

BUILT/PUBLISHING retry verifies candidate/proof and republishes through deterministic
ArtifactStore keys, then finalizes READY + audit. Revalidate source artifacts and current
bindings before publication/finalization and on replay. No completed response if source,
DB, storage or authorization is uncertain; partial artifacts remain unverified. Multiple
transactions cannot provide multi-object DB/FS atomicity. Manual orphan/retention cleanup
remains necessary; no M2.4 or background work. Private scratch cleanup validates the
resolved boundary; no arbitrary recursive deletion or changes to linked target files.

## Files and tests defined before implementation

Add hogwarts/patches/{contracts,inputs,git,builder}.py; tests/test_patches.py and
contracts/patch-build.v1.schema.json; docs/18-TRUSTED-GIT-PATCH-BUILDER.md and this ADR.
README scoped addition only; keep old models, migrations, CI and all tests byte-identical.
CI already builds sandbox/reader and discovers new tests. No remote CI pass claim.

Tests (SQLite and PostgreSQL where applicable):
1. Real temporary Git repositories; text additions/modifications, exact binary patch,
   ignored/Unicode/space/dash names, no changes/empty, same bytes/new path no rename,
   omitted files not deleted, executable base mode preserved and no mode input.
2. Manifest schema/ID/hash/key/binding/raw archive/file record corruption; stale Base,
   wrong owner/principal/run/import/lease, grant/repo/path revocation; no publication.
3. Traversal/symlink/hardlink/special/submodule/mode/case/prefix collisions; sentinels
   unchanged. Host env/config/hooks/filters cannot execute; argv/shell safety.
4. Concurrent independent builders, deterministic retries/reconstructed service, candidate
   tamper/partial IO, commit/publication failures and unavailable audit; no overwrite.
5. Git/export/apply mismatch, file identity/content/added-file TOCTOU and source artifact
   mutation/revocation during build; no completed result.
6. Real Linux sandbox -> secure importer -> builder focused integration, preserving
   unverified, no QA commands, and full 569-case existing regression on Linux/Windows.

Unverified until executed: remote CI, hostile OS admin/same-UID attacker, real power loss,
multiprocess stress, Windows kernel sandbox. Work directory is trusted private data
storage, NOT a sandbox. No M2.3.4/.5, M2.4, real Codex, commit/push/merge/deploy.


## Implementation / verification notes

Raw commit bodies are capped at 1 MiB and needed base blobs at 8 MiB/64 MiB using
cat-file -s BEFORE cat-file body reads. An explicit oversized-base test records that
no blob body read occurs. Source Git metadata checks still reuse unchanged M2.1.
The clean-base contract is index-based, not a checkout/QA runner. The binary test uses
real Git/DB/storage with deterministic fake sandbox capture; separate real Linux
integration uses the actual deterministic text output and destroyed-sandbox replay.

Idempotency journal namespace collision is denied without changing the foreign record.
Scratch/candidate failures and integrity mismatches fail closed; publication/DB commit
recovery retains deterministic keys. Physical process crash, multi-process stress, full
Git-output filesystem quotas, hostile same-UID actors and remote CI remain unverified.
See the acceptance report for actual commands, code fingerprints and raw/composite JUnit
provenance. Complete regression and final affected-suite runs are reported separately;
a late bounded-read hardening is validated by rerunning the entire new Patch suite.
