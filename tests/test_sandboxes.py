"""Fake-driver logic tests and explicitly opted-in REAL Linux kernel isolation probes."""

import io
import os
import sys
import tarfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from hogwarts.auth import Principal
from hogwarts.models import PrototypeRun, SandboxInstance, WorkflowEvent, now
from hogwarts.sandboxes.contracts import RuntimeProfile, SandboxError, deny
from hogwarts.sandboxes.controller import LinuxSandboxController
from hogwarts.sandboxes.driver import DockerDriver
from hogwarts.service import DomainError, utc
from sqlalchemy import select
from test_workspaces import workspace as workspace

IMAGE = "sha256:" + "a" * 64


class FakeDriver:
    """No isolation claims may be inferred from this test double."""

    def __init__(self):
        self.items = {}
        self.calls = []
        self.failure = None

    def action(self, name):
        self.calls.append(name)
        if name == self.failure:
            deny("injected_" + name)

    def create(self, row, profile):
        self.action("create")
        if row.id in self.items:
            deny("sandbox_partial_creation")
        self.items[row.id] = False
        return "f" * 64

    def verify(self, row, profile):
        self.action("verify")
        if row.id not in self.items:
            deny("sandbox_missing")
        return {"State": {"Running": self.items[row.id]}}

    def start(self, row, profile, archive):
        self.action("start")
        self.items[row.id] = True
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            members = stream.getmembers()
            assert all(m.isreg() or m.isdir() for m in members)
            assert all(m.uid == m.gid == 0 for m in members)
            assert all(m.mode in {0o444, 0o555} for m in members)
            assert not any(".git" == p for m in members for p in m.name.split("/"))
            assert not any(m.name == "README.md" for m in members)

    def execute(self, row, profile, seconds):
        self.action("execute")
        return {"exit_code": 0, "output": {"deterministic": True}}

    def stop(self, row):
        self.action("stop")
        if row.id in self.items:
            self.items[row.id] = False

    def destroy(self, row):
        self.action("destroy")
        self.items.pop(row.id, None)


@pytest.fixture
def sandbox(workspace):
    w = workspace
    info = w["manager"].create(w["actor"], w["run"], w["token"])
    driver = FakeDriver()
    profile = RuntimeProfile("verified", IMAGE)
    controller = LinuxSandboxController(w["manager"], driver, (profile,))
    return w, info, driver, controller


def create(s):
    w, info, _, controller = s
    return controller.create(w["actor"], w["run"], info.id, w["token"], "verified")


def start(s):
    w, _, _, controller = s
    return controller.start(w["actor"], w["run"], w["token"])


def execute(s, invocation="execute-1"):
    w, _, _, controller = s
    return controller.execute(w["actor"], w["run"], w["token"], invocation)


@pytest.mark.parametrize(
    "change",
    [
        {"name": "--privileged"},
        {"image": "python:latest"},
        {"command": "sh -c env"},
        {"cpus": 0},
        {"memory_mb": 0},
        {"pids": 10000},
        {"timeout_seconds": 61},
        {"lifetime_seconds": 100},
        {"command": "docker push"},
    ],
)
def test_profile_rejects_unsafe_options(change):
    with pytest.raises(SandboxError):
        replace(RuntimeProfile("verified", IMAGE), **change)


def test_windows_controller_is_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SandboxError, match="linux_controller_required"):
        DockerDriver("test")


def test_lifecycle_and_idempotent_results(sandbox):
    w, _, driver, controller = sandbox
    first = create(sandbox)
    assert first.state == "CREATED" and create(sandbox).id == first.id
    assert start(sandbox).state == start(sandbox).state == "RUNNING"
    result = execute(sandbox)
    assert result.state == "COMPLETED"
    assert execute(sandbox).result == result.result
    assert driver.calls.count("create") == driver.calls.count("execute") == 1
    assert not driver.items[first.id]
    with pytest.raises(SandboxError):
        execute(sandbox, "other")
    assert controller.stop(w["actor"], w["run"], w["token"]).state == "STOPPED"
    assert controller.stop(w["actor"], w["run"], w["token"]).state == "STOPPED"
    assert controller.destroy(w["actor"], w["run"], w["token"]).state == "DESTROYED"
    assert controller.destroy(w["actor"], w["run"], w["token"]).state == "DESTROYED"
    with pytest.raises(SandboxError):
        create(sandbox)


@pytest.mark.parametrize(
    "kind", ["token", "owner", "agent", "human", "workspace", "profile", "expired", "cancelled"]
)
def test_authorization_denials(sandbox, kind):
    w, info, driver, controller = sandbox
    actor, token, workspace_id, profile = w["actor"], w["token"], info.id, "verified"
    if kind == "token":
        token = "wrong"
    if kind == "owner":
        actor = replace(actor, owner_id="other")
    if kind == "agent":
        actor = replace(actor, id="worker:other")
    if kind == "human":
        actor = Principal("human:local-owner", "local-owner", "human")
    if kind == "workspace":
        workspace_id = "another-run-workspace"
    if kind == "profile":
        profile = "verified; docker run --privileged"
    if kind in {"expired", "cancelled"}:
        with w["factory"].begin() as session:
            run = session.get(PrototypeRun, w["run"])
            if kind == "expired":
                run.lease_expires_at = now() - timedelta(seconds=1)
            else:
                run.status = "CANCELLED"
    with pytest.raises(DomainError):
        controller.create(actor, w["run"], workspace_id, token, profile)
    assert "create" not in driver.calls
    with w["factory"]() as session:
        events = list(
            session.scalars(select(WorkflowEvent).where(WorkflowEvent.source == "sandbox_runtime"))
        )
        assert events and events[-1].event_type == "sandbox.denied"
        assert token not in str([e.payload for e in events])


@pytest.mark.parametrize("name", [".env", "id_rsa", "test.pem", ".ssh/key", "secrets/token"])
def test_sensitive_source_rejected(sandbox, name):
    _, info, driver, _ = sandbox
    path = info.path / "src" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("sensitive-sentinel")
    with pytest.raises(SandboxError, match="sandbox_sensitive_path"):
        create(sandbox)
    assert not driver.calls


def test_snapshot_changed_after_create_denied(sandbox):
    create(sandbox)
    (sandbox[1].path / "src/main.txt").write_text("new version")
    with pytest.raises(SandboxError, match="sandbox_snapshot_changed"):
        start(sandbox)
    assert "start" not in sandbox[2].calls


@pytest.mark.parametrize("phase", ["create", "verify", "start", "execute", "stop", "destroy"])
def test_injected_failures_audit_and_cleanup(sandbox, phase):
    w, _, driver, controller = sandbox
    if phase not in {"create", "verify"}:
        create(sandbox)
    if phase in {"execute", "stop", "destroy"}:
        start(sandbox)
    driver.failure = phase
    operation = create if phase in {"create", "verify"} else start if phase == "start" else execute
    with pytest.raises(SandboxError):
        if phase == "destroy":
            controller.destroy(w["actor"], w["run"], w["token"])
        else:
            operation(sandbox)
    driver.failure = None
    assert controller.destroy(w["actor"], w["run"], w["token"]).state == "DESTROYED"
    assert not driver.items
    with w["factory"]() as session:
        events = list(
            session.scalars(select(WorkflowEvent).where(WorkflowEvent.source == "sandbox_runtime"))
        )
        assert any("failed" in e.event_type or e.event_type == "sandbox.denied" for e in events)
        assert w["token"] not in str([e.payload for e in events])


def test_expired_lease_denies_execution_but_allows_cleanup(sandbox):
    w, _, _, controller = sandbox
    create(sandbox)
    start(sandbox)
    with w["factory"].begin() as session:
        session.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
    with pytest.raises(DomainError):
        execute(sandbox)
    assert controller.destroy(w["actor"], w["run"], w["token"]).state == "DESTROYED"


def test_concurrent_create_and_execute(sandbox):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(sandbox), range(2)))
    assert results[0].id == results[1].id
    start(sandbox)

    def attempt(_):
        try:
            return execute(sandbox).state
        except SandboxError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert "COMPLETED" in results
    assert sandbox[2].calls.count("execute") == 1


@pytest.fixture
def real_sandbox(workspace):
    if sys.platform != "linux" or not os.getenv("HOGWARTS_SANDBOX_IMAGE"):
        pytest.skip("REAL Linux Docker isolation requires HOGWARTS_SANDBOX_IMAGE; not verified")
    w = workspace
    info = w["manager"].create(w["actor"], w["run"], w["token"])
    driver = DockerDriver("m2-2-verification")
    controllers = []

    def build(command, **limits):
        profile = RuntimeProfile(
            "probe-" + command, os.environ["HOGWARTS_SANDBOX_IMAGE"], command, **limits
        )
        controller = LinuxSandboxController(w["manager"], driver, (profile,))
        controllers.append(controller)
        controller.create(w["actor"], w["run"], info.id, w["token"], profile.name)
        controller.start(w["actor"], w["run"], w["token"])
        return controller

    yield w, info, driver, build
    for controller in controllers:
        controller.destroy(Principal("human:local-owner", "local-owner", "human"), w["run"])


def test_real_linux_security_controls(real_sandbox):
    w, info, driver, build = real_sandbox
    controller = build("verify")
    with w["factory"]() as session:
        row = session.scalar(select(SandboxInstance))
        inspected = driver.verify(row, controller.profiles["probe-verify"])
        assert sorted(inspected["HostConfig"]["Ulimits"], key=lambda entry: entry["Name"]) == [
            {"Name": "core", "Hard": 0, "Soft": 0},
            {"Name": "nofile", "Hard": 128, "Soft": 128},
        ]
    result = controller.execute(w["actor"], w["run"], w["token"], "probe").result["output"]
    assert result["root_escalation_denied"]
    assert result["uid"] == result["gid"] == 10001
    assert int(result["cap_eff"], 16) == 0
    assert result["no_new_privs"] == "1" and result["seccomp"] == "2"
    assert result["network_blocked"] and result["interfaces"] == ["lo"]
    assert all(
        result[k]
        for k in [
            "readonly_root",
            "readonly_workspace",
            "git_absent",
            "host_secrets_unavailable",
            "readonly_shm",
        ]
    )
    assert "README.md" not in result["input_files"]
    assert "src/main.txt" in result["input_files"]
    assert not any(
        "TOKEN" in k or "PASSWORD" in k or "SSH" in k or "DOCKER" in k
        for k in result["environment"]
    )
    assert (info.path / "src/main.txt").read_bytes() == b"before\n"
    assert not (info.path / "prototype.txt").exists()


@pytest.mark.parametrize(
    "command,key", [("pids", "pids_denied"), ("disk", "disk_denied"), ("cpu", "cpu_max")]
)
def test_real_linux_resource_limits(real_sandbox, command, key):
    w, _, _, build = real_sandbox
    controller = build(command)
    result = controller.execute(w["actor"], w["run"], w["token"], "probe").result["output"]
    assert result[key]
    if command == "cpu":
        assert result["cpu_max"] == "50000 100000"
        before = dict(line.split() for line in result["before"].splitlines())
        after = dict(line.split() for line in result["after"].splitlines())
        assert int(after["nr_throttled"]) > int(before["nr_throttled"])


@pytest.mark.parametrize(
    "command,code",
    [
        ("timeout", "sandbox_timeout"),
        ("memory", "sandbox_command_failed"),
        ("fail", "sandbox_command_failed"),
    ],
)
def test_real_linux_failures_stop_descendants(real_sandbox, command, code):
    w, _, driver, build = real_sandbox
    controller = build(command, timeout_seconds=2 if command == "timeout" else 8)
    started = time.monotonic()
    with pytest.raises(SandboxError, match=code):
        controller.execute(w["actor"], w["run"], w["token"], "probe")
    assert time.monotonic() - started < 15
    with w["factory"]() as session:
        row = session.scalar(select(SandboxInstance))
        assert row.state == "FAILED"
        assert not driver.inspect(row)["State"]["Running"]
        if command == "memory":
            assert row.result["exit_code"] == 137


def test_real_linux_independent_lifetime(real_sandbox):
    w, _, driver, build = real_sandbox
    build("verify", timeout_seconds=1, lifetime_seconds=10)
    with w["factory"]() as session:
        expiry = utc(session.scalar(select(SandboxInstance)).expires_at)
    # Absolute lifetime includes allocation/initialization, which can take >3s under load.
    time.sleep(max(0, (expiry - now()).total_seconds()) + 0.3)
    with w["factory"]() as session:
        row = session.scalar(select(SandboxInstance))
        assert not driver.inspect(row)["State"]["Running"]


def test_uncertain_execution_never_replayed(sandbox):
    w, _, driver, _ = sandbox
    create(sandbox)
    start(sandbox)
    with w["factory"].begin() as session:
        row = session.scalar(select(SandboxInstance))
        row.state, row.invocation_id = "EXECUTING", "execute-1"
    with pytest.raises(SandboxError, match="sandbox_execution_conflict"):
        execute(sandbox)
    assert "execute" not in driver.calls


def test_cleanup_by_owner_and_wrong_lease_denied(sandbox):
    w, _, driver, controller = sandbox
    create(sandbox)
    with pytest.raises(DomainError):
        controller.destroy(w["actor"], w["run"], "wrong")
    with pytest.raises(DomainError):
        controller.destroy(Principal("human:other", "other", "human"), w["run"])
    assert driver.items
    owner = Principal("human:local-owner", "local-owner", "human")
    assert controller.destroy(owner, w["run"]).state == "DESTROYED"


def test_runtime_profile_change_denied_cleanup_still_allowed(sandbox):
    w, _, driver, controller = sandbox
    create(sandbox)
    changed = LinuxSandboxController(
        w["manager"], driver, (RuntimeProfile("verified", IMAGE, cpus=1),)
    )
    with pytest.raises(SandboxError, match="sandbox_profile_changed"):
        changed.start(w["actor"], w["run"], w["token"])
    assert changed.destroy(w["actor"], w["run"], w["token"]).state == "DESTROYED"


def test_partial_allocation_failure_is_cleanupable(sandbox, monkeypatch):
    w, _, driver, controller = sandbox
    allocate = driver.create

    def failed(row, profile):
        allocate(row, profile)
        deny("injected_after_allocation")

    monkeypatch.setattr(driver, "create", failed)
    with pytest.raises(SandboxError, match="injected_after_allocation"):
        create(sandbox)
    assert driver.items
    assert controller.destroy(w["actor"], w["run"], w["token"]).state == "DESTROYED"
    assert not driver.items


def test_source_link_denied(sandbox):
    w, info, driver, _ = sandbox
    if sys.platform == "win32":
        pytest.skip("native Windows symlink unavailable; Linux verifies this check")
    sentinel = w["root"].parent / "host-sentinel"
    sentinel.write_text("host sentinel")
    (info.path / "src/escape").symlink_to(sentinel)
    try:
        with pytest.raises(DomainError):
            create(sandbox)
        assert not driver.calls
        assert sentinel.read_text() == "host sentinel"
    finally:
        (info.path / "src/escape").unlink()


def test_cleaned_workspace_cannot_execute(sandbox):
    w, _, driver, _ = sandbox
    create(sandbox)
    start(sandbox)
    w["manager"].cleanup(w["actor"], w["run"], w["token"])
    with pytest.raises(SandboxError, match="sandbox_workspace_mismatch"):
        execute(sandbox)
    assert "execute" not in driver.calls


def test_real_linux_configuration_tamper_denied(real_sandbox, monkeypatch):
    import copy

    w, _, driver, build = real_sandbox
    controller = build("verify")
    with w["factory"]() as session:
        row = session.scalar(select(SandboxInstance))
        profile = controller.profiles["probe-verify"]
        original = driver.verify(row, profile)
        for key, value in [
            ("Privileged", True),
            ("NetworkMode", "host"),
            ("PidMode", "host"),
            ("IpcMode", "host"),
            ("CapAdd", ["SYS_ADMIN"]),
            ("ReadonlyRootfs", False),
            ("SecurityOpt", []),
            ("Memory", 0),
            ("Binds", ["/var/run/docker.sock:/var/run/docker.sock"]),
        ]:
            changed = copy.deepcopy(original)
            changed["HostConfig"][key] = value
            with monkeypatch.context() as patch:
                patch.setattr(driver, "inspect", lambda row, value=changed: value)
                with pytest.raises(SandboxError, match="sandbox_configuration_conflict"):
                    driver.verify(row, profile)
