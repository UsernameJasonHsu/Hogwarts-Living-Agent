"""Run-scoped local Git workspaces, reusing M1 ownership, leases and audit records."""

import hashlib
import os
import secrets
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from hogwarts.models import GitWorkspace, Project, PrototypeRun, WorkflowEvent, now
from hogwarts.service import DomainError, digest, proposal_for, utc
from hogwarts.workspaces.contracts import (
    RepositoryGrant,
    WorkspaceDiff,
    WorkspaceError,
    WorkspaceInfo,
)
from hogwarts.workspaces.git import LocalGit, reject, relative_path, safe_directory, within


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class GitWorkspaceManager:
    def __init__(self, factory, root: Path, grants: tuple[RepositoryGrant, ...], *, git=None):
        self.factory = factory
        candidate = safe_directory(root)
        for grant in grants:
            repo = safe_directory(grant.repository)
            if candidate == repo or candidate in repo.parents or repo in candidate.parents:
                reject("unsafe_workspace_root", 403)
        self.git = git or LocalGit(root)
        self.root = self.git.root
        self.grants = {grant.project_id: grant for grant in grants}
        if len(self.grants) != len(grants):
            reject("duplicate_repository_grant")

    def _event(self, session, actor, run_id, event, version, **payload):
        session.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=version,
                event_type=event,
                source="git_workspace",
                actor_id=actor.id,
                reason=event,
                payload=payload,
            )
        )

    def _audit_error(self, actor, run_id, code):
        with self.factory.begin() as session:
            self._event(session, actor, run_id, "workspace.denied", 0, code=code)

    def _authorize(self, session, actor, run_id, lease_token, *, cleanup=False):
        if actor.role not in ({"human", "worker"} if cleanup else {"worker"}):
            reject("worker_required", 403)
        run = session.get(PrototypeRun, run_id)
        if run is None:
            reject("not_found", 404)
        proposal = proposal_for(session, run.proposal_id, actor)
        project = session.get(Project, proposal.project_id)
        if not project or project.owner_id != actor.owner_id:
            reject("not_found", 404)
        if cleanup:
            from hogwarts.leases.fencing import lock_run

            lock_run(session, run)
        if cleanup and actor.role == "human":
            return run, proposal, project
        if (
            not lease_token
            or not run.lease_token
            or not secrets.compare_digest(token_hash(lease_token), token_hash(run.lease_token))
            or run.worker_id != actor.id
        ):
            reject("invalid_workspace_lease", 403)
        if not cleanup and (
            run.status != "CLAIMED"
            or proposal.status != "QUEUED"
            or run.lease_expires_at is None
            or utc(run.lease_expires_at) <= now()
        ):
            reject("inactive_workspace_lease", 403)
        if not cleanup:
            from hogwarts.leases.fencing import fence

            fence(session, run)
        return run, proposal, project

    def _configuration(self, proposal, project):
        grant = self.grants.get(project.id)
        if not grant or grant.repo_ref != project.repo_ref:
            reject("repository_not_granted", 403)
        repo = self.git.repository(grant.repository)
        operator_paths = tuple(relative_path(p) for p in grant.allowed_paths)
        project_paths = tuple(relative_path(p) for p in project.allowed_paths)
        allowed = tuple(relative_path(p) for p in proposal.document["prototype"]["allowed_paths"])
        if not allowed or any(
            not within(p, operator_paths) or not within(p, project_paths) for p in allowed
        ):
            reject("workspace_scope_not_granted", 403)
        commit = proposal.document["prototype"]["base_commit"]
        tree = self.git.tree(repo, commit)
        fingerprint = digest(
            {
                "repo": str(repo),
                "repo_ref": project.repo_ref,
                "project": project.id,
                "document": proposal.document,
                "operator_paths": operator_paths,
                "project_paths": project_paths,
            }
        )
        return repo, allowed, commit, fingerprint, tree

    def _path(self, row):
        # IDs come from our own UUID default, never a request path.
        import uuid

        try:
            if str(uuid.UUID(row.id)) != row.id:
                reject("workspace_identity_invalid", 403)
        except ValueError:
            reject("workspace_identity_invalid", 403)
        safe_directory(self.root)
        return self.root / row.id

    def _info(self, row):
        return WorkspaceInfo(
            row.id,
            row.run_id,
            self._path(row),
            row.base_commit,
            row.state,
            row.version,
            row.error_code,
        )

    def _row(self, session, actor, run_id):
        row = session.scalar(
            select(GitWorkspace).where(
                GitWorkspace.run_id == run_id, GitWorkspace.owner_id == actor.owner_id
            )
        )
        if row is None:
            reject("workspace_not_found", 404)
        # A real SQL write lock works across processes on both supported backends.
        session.execute(
            update(GitWorkspace)
            .where(GitWorkspace.id == row.id)
            .values(version=GitWorkspace.version)
        )
        session.refresh(row)
        return row

    def _state(self, session, row, actor, state, code=None):
        row.state, row.error_code = state, code
        row.version += 1
        row.updated_at = now()
        self._event(
            session,
            actor,
            row.run_id,
            "workspace." + state.lower(),
            row.version,
            workspace_id=row.id,
            code=code,
        )

    def create(self, actor, run_id, lease_token):
        try:
            return self._create(actor, run_id, lease_token)
        except DomainError as error:
            self._audit_error(actor, run_id, error.code)
            raise
        except (OSError, UnicodeError):
            self._audit_error(actor, run_id, "workspace_io_failed")
            raise WorkspaceError(409, "workspace_io_failed") from None

    def _create(self, actor, run_id, lease_token):
        # Durable intent is committed BEFORE touching Git/workspace files.
        try:
            with self.factory.begin() as session:
                _, proposal, project = self._authorize(session, actor, run_id, lease_token)
                repo, allowed, commit, fingerprint, _ = self._configuration(proposal, project)
                existing = session.scalar(select(GitWorkspace).where(GitWorkspace.run_id == run_id))
                if existing is None:
                    row = GitWorkspace(
                        run_id=run_id,
                        project_id=project.id,
                        owner_id=actor.owner_id,
                        repository_path=str(repo),
                        base_commit=commit,
                        allowed_paths=list(allowed),
                        request_hash=fingerprint,
                        lease_hash=token_hash(lease_token),
                    )
                    session.add(row)
                    session.flush()
                    self._event(
                        session, actor, run_id, "workspace.creating", 1, workspace_id=row.id
                    )
        except IntegrityError:
            # A competing intent won; reload it under the operation lock below.
            pass
        error = None
        with self.factory.begin() as session:
            _, proposal, project = self._authorize(session, actor, run_id, lease_token)
            row = self._row(session, actor, run_id)
            repo, allowed, commit, fingerprint, tree = self._configuration(proposal, project)
            if row.request_hash != fingerprint or row.lease_hash != token_hash(lease_token):
                reject("workspace_request_conflict")
            path = self._path(row)
            if row.state == "READY":
                self.git.provenance(repo, path, commit)
                return self._info(row)
            if row.state != "CREATING":
                reject("workspace_not_creatable")
            try:
                if os.path.lexists(path):
                    # Crash recovery adopts only the exact clean, registered pinned worktree.
                    self.git.provenance(repo, path, commit)
                    if self.git.snapshot(path, tree) != tree:
                        reject("workspace_recovery_conflict")
                else:
                    self.git.add(repo, path, commit)
                self._state(session, row, actor, "READY")
            except (DomainError, OSError, UnicodeError) as failure:
                error = failure.code if isinstance(failure, DomainError) else "workspace_io_failed"
                self._state(session, row, actor, "FAILED", error)
            result = self._info(row)
        if error:
            reject(error)
        return result

    def export_diff(self, actor, run_id, lease_token):
        try:
            with self.factory.begin() as session:
                _, proposal, project = self._authorize(session, actor, run_id, lease_token)
                row = self._row(session, actor, run_id)
                repo, allowed, commit, fingerprint, _ = self._configuration(proposal, project)
                if row.request_hash != fingerprint or row.state != "READY":
                    reject("workspace_request_conflict")
                changed, patch = self.git.diff(repo, self._path(row), commit, allowed)
                checksum = hashlib.sha256(patch).hexdigest()
                row.last_diff_sha256, row.last_changed_files = checksum, list(changed)
                row.version += 1
                row.updated_at = now()
                self._event(
                    session,
                    actor,
                    run_id,
                    "workspace.diff_exported",
                    row.version,
                    workspace_id=row.id,
                    sha256=checksum,
                    changed_files=list(changed),
                    size=len(patch),
                )
                return WorkspaceDiff(row.id, commit, changed, patch, checksum)
        except DomainError as error:
            self._audit_error(actor, run_id, error.code)
            raise
        except (OSError, UnicodeError):
            self._audit_error(actor, run_id, "workspace_io_failed")
            raise WorkspaceError(409, "workspace_io_failed") from None

    def cleanup(self, actor, run_id, lease_token=None):
        error = None
        try:
            with self.factory.begin() as session:
                self._authorize(session, actor, run_id, lease_token, cleanup=True)
                row = self._row(session, actor, run_id)
                if actor.role == "worker" and row.lease_hash != token_hash(lease_token):
                    reject("invalid_workspace_lease", 403)
                if row.state == "CLEANED":
                    return self._info(row)
                grant = self.grants.get(row.project_id)
                if not grant:
                    reject("repository_not_granted", 403)
                repo = self.git.repository(grant.repository)
                if str(repo) != row.repository_path:
                    reject("workspace_repository_changed", 403)
                self._state(session, row, actor, "CLEANING")
                try:
                    self.git.remove(repo, self._path(row))
                    self._state(session, row, actor, "CLEANED")
                except (DomainError, OSError, UnicodeError) as failure:
                    error = (
                        failure.code if isinstance(failure, DomainError) else "workspace_io_failed"
                    )
                    self._state(session, row, actor, "CLEANUP_FAILED", error)
                result = self._info(row)
            if error:
                reject(error)
            return result
        except DomainError as failure:
            self._audit_error(actor, run_id, failure.code)
            raise
        except (OSError, UnicodeError):
            self._audit_error(actor, run_id, "workspace_io_failed")
            raise WorkspaceError(409, "workspace_io_failed") from None
