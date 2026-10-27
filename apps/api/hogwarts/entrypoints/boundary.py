"""Trusted-process graph capabilities, not an OS sandbox or generated authority."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path

from hogwarts.auth import Principal
from hogwarts.entrypoints.contracts import FormalDispatch
from hogwarts.models import IdempotencyRecord, now
from hogwarts.reconciliation.contracts import deny
from hogwarts.service import DomainError, utc
from hogwarts.workspaces.contracts import WorkspaceError


@dataclass
class Permit:
    issuer: object
    actor: Principal
    run_id: str
    mode: str
    generation: int | None = None
    dispatch_id: str | None = None
    reservation_id: str | None = None
    expires_at: object = None
    active: bool = True
    gc_validated: bool = False
    gc_targets: tuple = ()
    gc_workspaces: tuple = ()
    token: str | None = field(default=None, repr=False)
    session: object = field(default=None, repr=False)


class FormalBoundary:
    def __init__(self, gateway):
        self.g = gateway
        self.current = ContextVar("formal_effect_permit", default=None)
        self.depth = ContextVar("formal_adapter_boundary", default=0)
        self.bindings = []
        self._execution_key = object()
        self._bind()

    def require(self, modes=None):
        p = self.current.get()
        if not p or p.issuer is not self or not p.active:
            deny("formal_effect_without_gate", 403)
        if modes and p.mode not in modes:
            deny("formal_effect_scope_denied", 403)
        if p.mode == "execute" and p.expires_at and utc(p.expires_at) <= now():
            deny("formal_lease_expired")
        return p

    @contextmanager
    def scope(self, actor, run_id, mode, *, _key=None, **values):
        if mode == "execute" and _key is not self._execution_key:
            deny("formal_execution_capability_required", 403)
        p = Permit(self, actor, run_id, mode, **values)
        token = self.current.set(p)
        try:
            yield p
        finally:
            p.active = False  # copied async contexts cannot retain authority
            self.current.reset(token)

    @contextmanager
    def execution_scope(self, actor, run_id, token, reservation):
        parent = self.require({"admission"})
        if not parent.dispatch_id or parent.actor != actor or parent.run_id != run_id:
            deny("formal_dispatch_required", 403)
        with self.g.factory.begin() as s:
            run, proposal, project = self.g.context(s, actor, run_id, token, live=True)
            row = s.get(IdempotencyRecord, parent.dispatch_id)
            if (
                not row
                or row.actor_id != "operations.v1"
                or not row.key.startswith("formal-dispatch.v1:")
            ):
                deny("formal_dispatch_required", 403)
            j = self.g.value(row, FormalDispatch, actor.owner_id)
            if (
                j.state != "DISPATCHING"
                or j.actor_id != actor.id
                or j.run_id != run_id
                or j.generation != run.lease_generation
                or j.configuration_sha256 != self.g.configuration(proposal, project)
            ):
                deny("formal_dispatch_binding_mismatch", 403)
            _, ledger = self.g.quota.ledger(s, actor, lock=False)
            held = self.g.quota.reservation(ledger, actor, run_id, reservation.reservation_id)
            if (
                held.state != "IN_USE"
                or held.generation != j.generation
                or held.profile != j.profile
            ):
                deny("formal_quota_in_use_required", 403)
            expiry = run.lease_expires_at
        with self.scope(
            actor,
            run_id,
            "execute",
            generation=reservation.generation,
            dispatch_id=parent.dispatch_id,
            reservation_id=reservation.reservation_id,
            expires_at=expiry,
            token=token,
            _key=self._execution_key,
        ):
            yield

    def session_check(self, s, actor, run, proposal, project, cleanup):
        p = self.require()
        if actor != p.actor or run.id != p.run_id:
            deny("formal_context_binding_mismatch", 403)
        if p.generation is None:
            p.generation = run.lease_generation
        if p.generation != run.lease_generation:
            deny("formal_generation_changed")
        p.session = s
        if p.mode == "stop":
            if (
                actor.role != "human"
                or run.lease_revoked_at is None
                or run.lease_stop_state not in {"PENDING", "FAILED", "CONFIRMED"}
            ):
                deny("formal_stop_intent_required", 403)
            return
        if p.mode == "execute":
            if self.g.orchestrator.preserve_budget is not True:
                deny("formal_budget_preservation_required", 403)
            row = s.get(IdempotencyRecord, p.dispatch_id)
            if not row:
                deny("formal_dispatch_required", 403)
            j = self.g.value(row, FormalDispatch, actor.owner_id)
            if (
                j.state != "DISPATCHING"
                or j.run_id != run.id
                or j.actor_id != actor.id
                or j.generation != run.lease_generation
                or j.configuration_sha256 != self.g.configuration(proposal, project)
            ):
                deny("formal_dispatch_binding_mismatch", 403)
            _, ledger = self.g.quota.ledger(s, actor, lock=False)
            r = self.g.quota.reservation(ledger, actor, run.id, p.reservation_id)
            if (
                r.state != "IN_USE"
                or r.generation != run.lease_generation
                or r.profile != j.profile
            ):
                deny("formal_quota_in_use_required", 403)
            self.g.monetary(s, actor, run, proposal)
            self.g.policy(s, proposal, project)
        elif p.mode == "admission" and not cleanup:
            self.g.monetary(s, actor, run, proposal)
            self.g.policy(s, proposal, project)

    @contextmanager
    def effect_transaction(self):
        p = self.require({"execute"})
        if p.session is not None and p.session.in_transaction():
            result = self._authorize_original(p.session, p.actor, p.run_id, p.token)
            self.session_check(p.session, p.actor, *result, False)
            yield
        else:
            with self.g.factory.begin() as s:
                result = self._authorize_original(s, p.actor, p.run_id, p.token)
                self.session_check(s, p.actor, *result, False)
                yield

    def wrap(self, obj, name, modes, *, driver=False, fresh=False):
        original = getattr(obj, name, None)
        if not callable(original):
            return

        @wraps(original)
        def guarded(*args, **kwargs):
            try:
                p = self.require(modes)
            except DomainError as error:
                if obj is self.g.workspaces:
                    raise WorkspaceError(error.status, error.code) from None
                raise
            for i, value in enumerate(args):
                if isinstance(value, Principal) and (
                    value != p.actor or (len(args) > i + 1 and args[i + 1] != p.run_id)
                ):
                    deny("formal_effect_binding_mismatch", 403)
            for value in (*args, *kwargs.values()):
                if hasattr(value, "run_id") and value.run_id != p.run_id:
                    deny("formal_effect_binding_mismatch", 403)
            if driver:
                marker = self.depth.set(self.depth.get() + 1)
                try:
                    if fresh and p.mode == "execute":
                        with self.effect_transaction():
                            return original(*args, **kwargs)
                    return original(*args, **kwargs)
                finally:
                    self.depth.reset(marker)
            if fresh and p.mode == "execute":
                with self.effect_transaction():
                    return original(*args, **kwargs)
            return original(*args, **kwargs)

        setattr(obj, name, guarded)
        self.bindings.append(type(obj).__name__ + "." + name)

    def docker(self, driver):
        original = getattr(driver, "call", None)
        if not callable(original):
            return

        @wraps(original)
        def call(*args, **kwargs):
            p = self.require()
            words = tuple(str(x) for x in args)
            if any(x == "prune" for x in words):
                deny("formal_prune_forbidden", 403)
            verb = (
                words[1]
                if words and words[0] in {"container", "volume", "image"} and len(words) > 1
                else words[0]
                if words
                else ""
            )
            readonly = verb in {"inspect", "ls", "version", "info"}
            if not readonly:
                if p.mode == "stop" and verb not in {"stop", "kill"}:
                    deny("formal_stop_only", 403)
                if p.mode == "cleanup" and verb not in {"rm", "stop", "kill"}:
                    deny("formal_cleanup_only", 403)
                if p.mode not in {"execute", "stop", "cleanup"} or not self.depth.get():
                    deny("formal_adapter_boundary_required", 403)
                if p.mode == "cleanup" and (not p.gc_validated or words[-1] not in p.gc_targets):
                    deny("formal_gc_target_not_approved", 403)
            return original(*args, **kwargs)

        driver.call = call
        self.bindings.append(type(driver).__name__ + ".call")

    def git(self, git):
        original = git.run

        @wraps(original)
        def run(repo, *args, **kwargs):
            p = self.require()
            verb = args[0] if args else ""
            if verb == "worktree" and len(args) > 1 and args[1] in {"add", "remove"}:
                allowed = {"execute"} if args[1] == "add" else {"execute", "cleanup"}
                if p.mode not in allowed:
                    deny("formal_git_effect_denied", 403)
                if p.mode == "cleanup" and (
                    not p.gc_validated or Path(args[-1]).name not in p.gc_workspaces
                ):
                    deny("formal_gc_target_not_approved", 403)
            elif verb == "worktree" and (len(args) < 2 or args[1] != "list"):
                deny("formal_git_operation_forbidden", 403)
            elif verb in {"read-tree", "add"}:
                index = kwargs.get("index")
                if p.mode != "execute" or not index or not Path(index).is_relative_to(git.root):
                    deny("formal_git_effect_denied", 403)
            elif verb == "config":
                if not any(str(x).startswith("--get") or x == "--list" for x in args[1:]):
                    deny("formal_git_operation_forbidden", 403)
            elif verb not in {
                "rev-parse",
                "symbolic-ref",
                "ls-tree",
                "cat-file",
                "diff",
                "status",
                "show",
                "worktree",
                "rev-list",
                "ls-files",
                "diff-index",
            }:
                deny("formal_git_operation_forbidden", 403)
            return original(repo, *args, **kwargs)

        git.run = run
        self.bindings.append(type(git).__name__ + ".run")

    def _bind(self):
        g, o = self.g, self.g.orchestrator
        w = g.workspaces
        if getattr(w, "_formal_boundary", None):
            deny("formal_graph_already_bound", 403)
        w._formal_boundary = self
        authorize = w._authorize
        self._authorize_original = authorize

        @wraps(authorize)
        def checked(s, actor, run_id, token, *, cleanup=False):
            self.require()
            result = authorize(s, actor, run_id, token, cleanup=cleanup)
            self.session_check(s, actor, *result, cleanup)
            return result

        w._authorize = checked
        for name in ("create", "export_diff"):
            self.wrap(w, name, {"execute"})
        self.wrap(w, "cleanup", {"execute", "cleanup"})
        for name in ("create", "start", "execute"):
            self.wrap(o.controller, name, {"execute"})
        self.wrap(o.controller, "stop", {"execute", "stop", "cleanup"})
        self.wrap(o.controller, "destroy", {"execute", "cleanup"})
        for obj, names in (
            (o, ("verify", "resume_publication")),
            (o.importer, ("import_output", "resume_publication")),
            (o.builder, ("build", "resume_publication")),
            (o.qa, ("run", "resume_publication")),
            (o.store, ("put",)),
        ):
            for name in names:
                self.wrap(obj, name, {"execute"})
        self.wrap(o.qa, "cleanup", {"execute", "cleanup"})
        for name in ("initialize", "release"):
            self.wrap(g.quota, name, {"admin"})
        for name in ("reserve", "receipt", "_transition"):
            self.wrap(g.quota, name, {"admission"})
        seen = set()
        for objects in (
            o.store.objects,
            o.builder.objects,
            o.importer.vault.objects,
            o.qa.vault.objects,
            o.vault.objects,
        ):
            if id(objects) in seen:
                continue
            seen.add(id(objects))
            self.wrap(objects, "publish", {"execute"}, fresh=True)
            self.wrap(objects, "read", {"execute", "inspect", "cleanup", "admin"})
        for driver in (o.controller.driver, o.qa.driver):
            for name in ("create", "start", "execute", "capture", "export_output", "_stream"):
                self.wrap(driver, name, {"execute"}, driver=True, fresh=True)
            self.wrap(driver, "stop", {"execute", "stop", "cleanup"}, driver=True)
            self.wrap(driver, "destroy", {"execute", "cleanup"}, driver=True)
            self.docker(driver)
        self.git(w.git)
        self.git(o.builder.git.config)
        self.wrap(o.builder.git, "construct", {"execute"})
        self.wrap(o.builder.git, "invoke", {"execute"})
        invoke = o.builder.git.invoke

        @wraps(invoke)
        def private_git(scratch, *args, **kwargs):
            if not args or args[0] not in {
                "init",
                "update-index",
                "hash-object",
                "write-tree",
                "read-tree",
                "ls-files",
                "diff",
                "apply",
                "cat-file",
                "--version",
            }:
                deny("formal_git_operation_forbidden", 403)
            return invoke(scratch, *args, **kwargs)

        o.builder.git.invoke = private_git
        if g.retention.backend:
            self.wrap(g.retention.backend, "remove", {"cleanup"}, driver=True)
