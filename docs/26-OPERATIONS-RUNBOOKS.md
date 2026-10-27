# Capacity / Cleanup / Recovery Runbooks — M2.4.4

Trusted owner/operator procedure only. No Agent administration tools or scheduler.
No commit/push/merge/deploy is part of these procedures. Windows is a development
host; Linux container results are separate from native Linux production validation.

## Capacity and admission

1. Pin existing runtime/reader/QA image IDs and repository grants. Choose a named
   QuotaProfile in trusted code; Agent only selects that name. CPU/memory/PID demand
   must cover the approved workload allocations; include conservative transient
   workspace/output storage headroom. Operator limits are immutable per ledger hash.
2. Configure owner-exclusive absolute private storage roots, outside source repos,
   covering workspace, ArtifactStore and every Import/Patch/QA/Verification capsule
   root. Shared, overlapping, symlink/reparse/hardlink/special-file scopes are denied.
3. Construct AggregateResourceQuotaManager and call initialize(owner_human, run_id).
   This requires the current owner/repository grant and persists a versioned ledger.
   No Agent may initialize/change limits or pass dimensions/paths at reserve time.
4. Use OperationalPrototypeController.run(worker, run, key, original_lease, profile)
   for new operational executions. It reserves before workload effects and persists
   IN_USE intent. Existing low-level controllers remain trusted-internal; mandatory
   deployment-wide wiring is not established by merely having this wrapper.
5. On quota_capacity_exceeded or quota_disk_headroom_exhausted, stop admission.
   Inspect owner-only OperationalHealth, held capacity, logical bytes, declared and
   unmaterialized Artifact bytes, free space and unknown reservations. Monetary
   reserved estimates and known actual records are separate; do not add estimates
   to spend or treat unknown actuals as zero. Fix capacity through reviewed operator
   configuration/migration, not ad-hoc ledger edits, fabricated receipts or resets.
6. IN_USE/UNKNOWN is held across restart/expiry. Never rerun from this intent.
   Investigate via M2.4.3 Recovery and current journals. Explicit owner-human release
   requires fresh exact resource absence, inactive lease, current grant and ledger
   version. Retained permanent evidence continues consuming observed storage.
7. Ledger history is capped at 1000 reservations per owner, fails closed at capacity,
   and is not pruned. Operator-led versioned archival/migration is a future step.

These are conservative controller admission limits, not filesystem/kernel hard
quota or physical Docker-volume byte accounting. Disk free/inode checks and ENOSPC
fault injection do not prove protection against every external writer, daemon
overhead or host disk exhaustion. Hard storage isolation, exclusive production
wiring and measured capacity/load validation remain readiness gates.

## Retention / reviewed ephemeral cleanup

1. Register VersionedRetentionPolicy in trusted code. It affects only exact known
   ephemeral container/volume/worktree resources. Artifact objects, Review Bundles,
   journals, source repos and all recovery capsules/candidates stay permanent KEEP.
2. Call SafeGarbageCollectionPlanner(..., backend=existing_DockerResourceBackend,
   store=existing_ArtifactStore).plan(owner_human, run_id, request_key). This is the
   default dry-run. Missing Store verifier for existing evidence blocks approval.
3. Read eligible/reasons, exact underlying CleanupJournal, retention deadline,
   generation/version, reference snapshot and resource fingerprint. Active lease,
   pending/deferred review, explicit Hold, UNKNOWN execution, unresolved references,
   bad Artifact bytes/metadata, uncertain IO/daemon and quarantine all block cleanup.
4. For incident/review/legal/operator preservation use ReferenceProtector.hold with
   expected Hold version (0 for first creation). Clear only through explicit owner
   action with current version; this grants no approval or execution rights.
5. Approve the exact GC plan/version explicitly as owner-human. Approval is finite,
   bound to actor/policy/references/resources, and is NOT a merge/deploy approval.
6. Execute approved version. Reconciler checks references/Holds inside its Run lock
   before each exact deletion, then checks identity/attachments again. Only immutable
   container ID / verified exact volume / registered worktree is removed. Never use
   prune or discover-by-prefix deletion. A Hold ordered before the deletion lock
   blocks deletion; a Hold requested after completion cannot undo prior effects.
7. Preserve UNKNOWN/partial completion evidence. Never replay an uncertain GC intent;
   investigate and create a NEW dry-run for remaining exact resources. A completed
   cached response is historical, not renewed permission. No automatic background GC.

## Recovery and dependency incident response

- DB unavailable: deny writes/execution; audit may also be unavailable. Do not claim
  a persisted failure record. Restore DB connectivity and inspect durable state.
- Docker unavailable: do not infer resource absence or release capacity. Restore
  connectivity before exact inventory. Live daemon restart was not validated here.
- Storage unavailable/full/integrity mismatch: stop admission/GC; preserve files,
  metadata and reservations. Repair infrastructure under separate approved operator
  procedures. No overwrite, object unlink, assessment promotion or inferred success.
- Worker/controller crash: M2.4.3 may resume only authenticated sealed publication.
  UNKNOWN workload is never replayed. M2.4.2 cleanup is distinct from workflow
  recovery, budget settlement and resource-quota release; each has its own proof.
- Health unknown: missing metrics are unknown, not zero. Snapshot scope is configured
  owner roots plus current Run; it is not a global all-daemon inventory or M3 signoff.

## Pre-existing quarantine: separate manual incident

ONE stopped container and THREE volumes from interrupted M2.4.3 tests have no trusted
DB ownership binding. Exact identities are in the delivery manual inventory. They
are not GC fixtures, not adopted or mounted, and are never auto-cleaned. Keep them
on the owner intervention list. Any eventual disposition requires provenance
investigation and a separately authorized exact-resource operation. No resolution
is claimed in M2.4.4. M3 readiness remains blocked while this incident is unresolved.

## M3 readiness boundary

This milestone implements operational components, not M3–M5 or real Codex. Require
fresh full JUnit, migrations and Ruff; remote CI verification; quarantine disposition;
native Linux/storage-hard-limit/load/backup-restore validation; mandatory production
admission wiring; current grants/approved images and an independent security review.
No generated report can grant permissions or substitute for those checks.
