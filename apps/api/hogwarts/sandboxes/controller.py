"""Trusted single-operator controller. No background loop and no M1 adapter replacement."""

import hashlib
import io
import re
import tarfile
from dataclasses import asdict
from datetime import timedelta
from types import MappingProxyType

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from hogwarts.models import GitWorkspace, SandboxInstance, WorkflowEvent, now
from hogwarts.sandboxes.contracts import SandboxError, SandboxInfo, deny
from hogwarts.service import DomainError, digest, utc
from hogwarts.workspaces.git import relative_path, walk_files, within
from hogwarts.workspaces.manager import token_hash


class LinuxSandboxController:
    def __init__(self, workspace_manager, driver, profiles):
        self.workspaces = workspace_manager
        self.factory = workspace_manager.factory
        self.driver = driver
        self.profiles = MappingProxyType({p.name: p for p in profiles})
        if len(self.profiles) != len(profiles):
            deny("duplicate_runtime_profile", 403)

    def _event(self, session, actor, run_id, event, version=0, **payload):
        session.add(
            WorkflowEvent(
                owner_id=actor.owner_id,
                aggregate_id=run_id,
                aggregate_version=version,
                event_type="sandbox." + event,
                source="sandbox_runtime",
                actor_id=actor.id,
                reason=event,
                payload=payload,
            )
        )

    def _audit(self, actor, run_id, code):
        with self.factory.begin() as session:
            self._event(session, actor, run_id, "denied", code=code)

    def _guard(self, actor, run_id, function):
        try:
            return function()
        except DomainError as error:
            self._audit(actor, run_id, error.code)
            raise
        except (OSError, UnicodeError, ValueError):
            self._audit(actor, run_id, "sandbox_io_failed")
            raise SandboxError(409, "sandbox_io_failed") from None

    def _info(self, row):
        return SandboxInfo(
            row.id, row.run_id, row.workspace_id, row.state, row.error_code, row.result
        )

    def _state(self, session, actor, row, state, code=None):
        row.state, row.error_code = state, code
        row.version += 1
        self._event(
            session,
            actor,
            row.run_id,
            state.lower(),
            version=row.version,
            sandbox_id=row.id,
            code=code,
        )

    def _row(self, session, actor, run_id, token, cleanup=False):
        run, proposal, project = self.workspaces._authorize(
            session, actor, run_id, token, cleanup=cleanup
        )
        row = session.scalar(
            select(SandboxInstance).where(
                SandboxInstance.run_id == run_id, SandboxInstance.owner_id == actor.owner_id
            )
        )
        if row is None:
            deny("sandbox_not_found", 404)
        session.execute(
            update(SandboxInstance)
            .where(SandboxInstance.id == row.id)
            .values(state=SandboxInstance.state)
        )
        session.refresh(row)
        # Recheck lease after acquiring the operation lock.
        self.workspaces._authorize(session, actor, run_id, token, cleanup=cleanup)
        if actor.role != "human" and row.lease_hash != token_hash(token):
            deny("sandbox_lease_changed", 403)
        if not cleanup:
            workspace = session.get(GitWorkspace, row.workspace_id)
            if (
                not workspace
                or workspace.run_id != run_id
                or workspace.owner_id != actor.owner_id
                or workspace.state != "READY"
                or workspace.lease_hash != row.lease_hash
            ):
                deny("sandbox_workspace_mismatch", 403)
        return row, run, proposal, project

    def _profile(self, row):
        profile = self.profiles.get(row.profile_name)
        if not profile or digest(asdict(profile)) != row.request_hash:
            deny("sandbox_profile_changed", 403)
        return profile

    def _snapshot(self, session, actor, run_id, token, workspace_id):
        _, proposal, project = self.workspaces._authorize(session, actor, run_id, token)
        # Reuse M2.1's SQL operation lock and validated repository/path grants.
        workspace = self.workspaces._row(session, actor, run_id)
        if workspace.id != workspace_id or workspace.state != "READY":
            deny("sandbox_workspace_mismatch", 403)
        if workspace.lease_hash != token_hash(token):
            deny("sandbox_lease_changed", 403)
        repo, allowed, commit, fingerprint, tree = self.workspaces._configuration(proposal, project)
        if workspace.request_hash != fingerprint:
            deny("sandbox_workspace_changed", 403)
        path = self.workspaces._path(workspace)
        self.workspaces.git.provenance(repo, path, commit)
        before = self.workspaces.git.snapshot(path, tree)
        files = walk_files(path, git_file=True)
        archive = io.BytesIO()
        directories = set()
        total = 0
        with tarfile.open(fileobj=archive, mode="w") as stream:
            for rel in sorted(files):
                relative_path(rel)
                if not within(rel, allowed):
                    continue
                parts = rel.casefold().split("/")
                if any(
                    p
                    in {
                        ".ssh",
                        ".aws",
                        ".azure",
                        ".docker",
                        "secrets",
                        "id_rsa",
                        "id_ed25519",
                        "credentials",
                        "credentials.json",
                    }
                    or p.startswith(".env")
                    or p.endswith((".pem", ".key", ".p12", ".pfx"))
                    for p in parts
                ):
                    deny("sandbox_sensitive_path", 403)
                for length in range(1, len(parts)):
                    name = "/".join(rel.split("/")[:length])
                    if name not in directories:
                        entry = tarfile.TarInfo(name)
                        entry.type, entry.mode = tarfile.DIRTYPE, 0o555
                        stream.addfile(entry)
                        directories.add(name)
                content = (path / rel).read_bytes()
                total += len(content)
                if total > 48 * 1024 * 1024 or len(content) > 8 * 1024 * 1024:
                    deny("sandbox_snapshot_too_large")
                entry = tarfile.TarInfo(rel)
                entry.mode, entry.size = 0o444, len(content)
                stream.addfile(entry, io.BytesIO(content))
        if self.workspaces.git.snapshot(path, tree) != before:
            deny("sandbox_snapshot_changed")
        data = archive.getvalue()
        if len(data) > 60 * 1024 * 1024:
            deny("sandbox_snapshot_too_large")
        return data, hashlib.sha256(data).hexdigest()

    def create(self, actor, run_id, workspace_id, lease_token, profile):
        return self._guard(
            actor, run_id, lambda: self._create(actor, run_id, workspace_id, lease_token, profile)
        )

    def _create(self, actor, run_id, workspace_id, token, profile_name):
        profile = self.profiles.get(profile_name)
        if not profile:
            deny("unapproved_runtime_profile", 403)
        try:
            with self.factory.begin() as session:
                run, proposal, _ = self.workspaces._authorize(session, actor, run_id, token)
                _, checksum = self._snapshot(session, actor, run_id, token, workspace_id)
                row = session.scalar(
                    select(SandboxInstance).where(SandboxInstance.run_id == run_id)
                )
                if row is None:
                    lifetime = min(
                        profile.lifetime_seconds,
                        proposal.document["prototype"]["max_runtime_seconds"],
                        int((utc(run.lease_expires_at) - now()).total_seconds()),
                    )
                    if lifetime < 2:
                        deny("sandbox_lease_too_short", 403)
                    row = SandboxInstance(
                        run_id=run_id,
                        workspace_id=workspace_id,
                        owner_id=actor.owner_id,
                        lease_hash=token_hash(token),
                        profile_name=profile.name,
                        request_hash=digest(asdict(profile)),
                        snapshot_sha256=checksum,
                        lifetime_seconds=lifetime,
                        expires_at=now() + timedelta(seconds=lifetime),
                    )
                    session.add(row)
                    session.flush()
                    self._event(session, actor, run_id, "creating", version=1, sandbox_id=row.id)
        except IntegrityError:
            pass
        error = None
        with self.factory.begin() as session:
            row, *_ = self._row(session, actor, run_id, token)
            _, checksum = self._snapshot(session, actor, run_id, token, workspace_id)
            if (
                row.workspace_id != workspace_id
                or row.profile_name != profile_name
                or row.snapshot_sha256 != checksum
            ):
                deny("sandbox_request_conflict")
            self._profile(row)
            if row.state in {"CREATED", "RUNNING", "COMPLETED"}:
                self.driver.verify(row, profile)
                return self._info(row)
            if row.state != "CREATING":
                deny("sandbox_not_creatable")
            try:
                row.container_id = self.driver.create(row, profile)
                self.driver.verify(row, profile)
                self._state(session, actor, row, "CREATED")
            except DomainError as failure:
                error = failure.code
                self._state(session, actor, row, "FAILED", error)
            answer = self._info(row)
        if error:
            deny(error)
        return answer

    def start(self, actor, run_id, lease_token):
        return self._guard(actor, run_id, lambda: self._start(actor, run_id, lease_token))

    def _start(self, actor, run_id, token):
        error = None
        with self.factory.begin() as session:
            row, *_ = self._row(session, actor, run_id, token)
            profile = self._profile(row)
            if row.state == "RUNNING":
                if not self.driver.verify(row, profile)["State"]["Running"]:
                    deny("sandbox_not_running")
                return self._info(row)
            if row.state != "CREATED":
                deny("sandbox_not_startable")
            archive, checksum = self._snapshot(session, actor, run_id, token, row.workspace_id)
            if checksum != row.snapshot_sha256:
                deny("sandbox_snapshot_changed")
            try:
                if utc(row.expires_at) <= now():
                    deny("sandbox_expired", 403)
                self.driver.start(row, profile, archive)
                self._state(session, actor, row, "RUNNING")
            except DomainError as failure:
                error = failure.code
                self._state(session, actor, row, "FAILED", error)
                try:
                    self.driver.stop(row)
                except DomainError:
                    error = "sandbox_stop_failed"
                    self._state(session, actor, row, "FAILED", error)
            answer = self._info(row)
        if error:
            deny(error)
        return answer

    def execute(self, actor, run_id, lease_token, invocation_id):
        return self._guard(
            actor, run_id, lambda: self._execute(actor, run_id, lease_token, invocation_id)
        )

    def _execute(self, actor, run_id, token, invocation):
        if not isinstance(invocation, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", invocation):
            deny("invalid_invocation_id", 403)
        with self.factory.begin() as session:
            row, *_ = self._row(session, actor, run_id, token)
            if row.invocation_id:
                if row.invocation_id == invocation and row.state == "COMPLETED":
                    return self._info(row)
                deny("sandbox_execution_conflict")
            if row.state != "RUNNING":
                deny("sandbox_not_running")
            self._profile(row)
            row.invocation_id = invocation
            self._state(session, actor, row, "EXECUTING")
        error = None
        with self.factory.begin() as session:
            row, run, *_ = self._row(session, actor, run_id, token)
            profile = self._profile(row)
            try:
                seconds = min(
                    profile.timeout_seconds,
                    (utc(row.expires_at) - now()).total_seconds(),
                    (utc(run.lease_expires_at) - now()).total_seconds(),
                )
                if seconds <= 0:
                    deny("sandbox_expired", 403)
                row.result = self.driver.execute(row, profile, seconds)
                if row.result["exit_code"]:
                    deny("sandbox_command_failed")
                self._state(session, actor, row, "COMPLETED")
            except DomainError as failure:
                error = failure.code
                self._state(session, actor, row, "FAILED", error)
            # Always terminate all remaining descendants, even on success.
            try:
                self.driver.stop(row)
            except DomainError:
                error = "sandbox_stop_failed"
                self._state(session, actor, row, "FAILED", error)
            answer = self._info(row)
        if error:
            deny(error)
        return answer

    def stop(self, actor, run_id, lease_token=None):
        return self._guard(actor, run_id, lambda: self._cleanup(actor, run_id, lease_token, False))

    def destroy(self, actor, run_id, lease_token=None):
        return self._guard(actor, run_id, lambda: self._cleanup(actor, run_id, lease_token, True))

    def _cleanup(self, actor, run_id, token, destroy):
        error = None
        with self.factory.begin() as session:
            row, *_ = self._row(session, actor, run_id, token, cleanup=True)
            if row.state == "DESTROYED" or (not destroy and row.state == "STOPPED"):
                return self._info(row)
            try:
                (self.driver.destroy if destroy else self.driver.stop)(row)
                self._state(session, actor, row, "DESTROYED" if destroy else "STOPPED")
            except DomainError as failure:
                error = failure.code
                self._state(session, actor, row, "CLEANUP_FAILED", error)
            answer = self._info(row)
        if error:
            deny(error)
        return answer
