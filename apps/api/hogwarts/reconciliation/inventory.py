"""Bounded DB/private-root inventory. No arbitrary paths and no evidence deletion."""

import hashlib
import os

from sqlalchemy import select

from hogwarts.models import ArtifactMetadata, GitWorkspace, IdempotencyRecord, OutputImport
from hogwarts.qa.profiles import safe_source_path
from hogwarts.reconciliation.contracts import Resource, deny
from hogwarts.reconciliation.identity import StateComparator
from hogwarts.service import DomainError, digest
from hogwarts.workspaces.git import plain, safe_directory, walk_files
from hogwarts.workspaces.manager import token_hash


def bounded_hash(path, expected):
    """No-follow descriptor read; compare identity before and after bounded IO."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def identity(x):
        return (x.st_dev, x.st_ino, x.st_mode, x.st_nlink, x.st_size, x.st_mtime_ns)

    before = plain(path)
    if identity(before) != identity(expected):
        deny("cleanup_workspace_changed")
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            deny("cleanup_workspace_changed")
        data = stream.read(before.st_size + 1)
        if (
            len(data) != before.st_size
            or identity(os.fstat(stream.fileno())) != identity(before)
            or identity(plain(path)) != identity(before)
        ):
            deny("cleanup_workspace_changed")
    return hashlib.sha256(data).hexdigest()


class ResourceInventory:
    def __init__(self, workspaces, backend=None):
        self.workspaces, self.backend = workspaces, backend

    def worktree(self, ws):
        m = self.workspaces
        path = m._path(ws)
        grant = m.grants.get(ws.project_id)
        if not grant:
            deny("cleanup_repository_not_granted", 403)
        repo = m.git.repository(grant.repository)
        if str(repo) != ws.repository_path:
            deny("cleanup_repository_mismatch", 403)
        if not os.path.lexists(path):
            # Missing tree with remaining registration requires investigation.
            if os.path.lexists(repo / ".git/worktrees" / ws.id):
                deny("cleanup_missing_registered_tree")
            return None
        if ws.state == "CLEANED":
            deny("cleanup_resurrected_worktree")
        m.git.provenance(repo, path, ws.base_commit)
        root = plain(path, directory=True)
        tree = {}
        total = 0
        for name, st in sorted(walk_files(path, git_file=True).items()):
            safe_source_path(name)
            total += st.st_size
            if st.st_size > 8 * 1024 * 1024 or total > 64 * 1024 * 1024:
                deny("cleanup_workspace_too_large")
            tree[name] = [st.st_ino, st.st_mode, bounded_hash(path / name, st)]
        return Resource(
            key="worktree:" + ws.id,
            kind="worktree",
            identifier=ws.id,
            fingerprint=digest(
                {
                    "root": [root.st_dev, root.st_ino],
                    "tree": tree,
                    "git": bounded_hash(path / ".git", plain(path / ".git")),
                    "repo": str(repo),
                    "base": ws.base_commit,
                }
            ),
            disposition="CLEANUP_ELIGIBLE",
            reason="identity_confirmed",
        )

    def inspect(self, s, run, proposal, project):
        m = self.workspaces
        resources = []
        ws = s.scalar(select(GitWorkspace).where(GitWorkspace.run_id == run.id))
        binding = {
            "run": run.id,
            "status": run.status,
            "generation": run.lease_generation,
            "lease_version": run.lease_version,
            "issued": run.lease_issued_generation,
            "expires": str(run.lease_expires_at),
            "revoked": str(run.lease_revoked_at),
            "project": project.id,
            "repo_ref": project.repo_ref,
            "paths": project.allowed_paths,
            "proposal": proposal.document,
        }
        grant = m.grants.get(project.id)
        if not grant or grant.repo_ref != project.repo_ref:
            deny("cleanup_repository_not_granted", 403)
        repo = m.git.repository(grant.repository)
        binding["grant"] = [str(repo), grant.repo_ref, grant.allowed_paths]
        safe_directory(m.root)
        root_info = plain(m.root, directory=True)
        binding["private_root"] = [str(m.root), root_info.st_dev, root_info.st_ino]
        if ws:
            if (
                ws.owner_id != project.owner_id
                or ws.project_id != project.id
                or ws.repository_path != str(repo)
                or ws.lease_hash != token_hash(run.lease_token or "")
            ):
                deny("cleanup_workspace_binding_mismatch", 403)
            binding["workspace"] = [ws.id, ws.version, ws.state, ws.base_commit, ws.request_hash]
            try:
                item = self.worktree(ws)
                if item:
                    resources.append(item)
            except (DomainError, OSError, UnicodeError) as e:
                resources.append(
                    Resource(
                        key="worktree:" + ws.id,
                        kind="worktree",
                        identifier=ws.id,
                        fingerprint=digest("unsafe"),
                        disposition="QUARANTINE",
                        reason=e.code if isinstance(e, DomainError) else "workspace_io_failed",
                    )
                )
        known = set(s.scalars(select(GitWorkspace.id))) | {".manager"}
        entries = list(m.root.iterdir())
        if len(entries) > 10000:
            deny("cleanup_inventory_limit")
        for entry in entries:
            if entry.name not in known:
                identifier = digest(entry.name)
                resources.append(
                    Resource(
                        key="unknown-root:" + identifier,
                        kind="unknown",
                        identifier=identifier,
                        fingerprint=identifier,
                        disposition="QUARANTINE",
                        reason="unknown_private_root_entry",
                    )
                )
        imports = list(s.scalars(select(OutputImport).where(OutputImport.run_id == run.id)))
        binding["imports"] = sorted([x.id, x.state, x.version] for x in imports)
        journals = []
        for row in s.scalars(
            select(IdempotencyRecord).where(IdempotencyRecord.owner_id == project.owner_id)
        ):
            if (
                row.key.startswith(
                    ("patch-builder.v1:", "qa-runner.v1:", "prototype-verification.v1:")
                )
                and row.response.get("run_id") == run.id
            ):
                journals.append([row.id, row.key, digest(row.response)])
        # SQL row order is not identity; use the same canonical sequence everywhere.
        journals.sort()
        binding["journals"] = journals
        artifacts = list(
            s.scalars(select(ArtifactMetadata).where(ArtifactMetadata.run_id == run.id))
        )
        for row in artifacts:
            resources.append(
                Resource(
                    key="artifact:" + row.id,
                    kind="evidence",
                    identifier=row.id,
                    fingerprint=digest([row.sha256, row.metadata_sha256, row.state]),
                    disposition="KEEP",
                    reason="permanent_evidence",
                )
            )
        resources.append(
            Resource(
                key="recovery-evidence:" + run.id,
                kind="evidence",
                identifier=run.id,
                fingerprint=digest(journals),
                disposition="KEEP",
                reason="recovery_capsules_and_journals",
            )
        )
        if self.backend:
            resources.extend(self.backend.inventory(s, run, ws))
        else:
            from hogwarts.models import SandboxInstance

            if s.scalar(select(SandboxInstance.id).where(SandboxInstance.run_id == run.id)):
                resources.append(
                    Resource(
                        key="unconfigured-docker",
                        kind="unknown",
                        identifier=run.id,
                        fingerprint=digest("missing_backend"),
                        disposition="QUARANTINE",
                        reason="docker_backend_required",
                    )
                )
        if len({x.key for x in resources}) != len(resources) or len(resources) > 10000:
            deny("cleanup_ambiguous_inventory")
        return digest(binding), StateComparator.classify(run, resources)
