"""Linux Docker only; fixed commands and bounded attach transport, never host tests."""

import json
import os
import subprocess
import threading
import time
from datetime import datetime

from hogwarts.qa.contracts import MAX_LOG, ExecutionResult, deny
from hogwarts.sandboxes.driver import DockerDriver
from hogwarts.service import now, utc


def container_argv(row, profile):
    return [
        "/usr/local/bin/python",
        "-I",
        "-B",
        "/opt/hogwarts/qa-supervisor.py",
        "--run",
        profile.command,
        profile.test_root,
        str(profile.timeout_seconds),
        str(int(utc(row.expires_at).timestamp())),
    ]


def environment_entries(entries):
    """Docker may reorder Env; duplicate names remain ambiguous and are forbidden."""
    if not isinstance(entries, list):
        deny("qa_invalid_environment", 403)
    result = {}
    for entry in entries:
        if not isinstance(entry, str) or "=" not in entry or "\0" in entry:
            deny("qa_invalid_environment", 403)
        name, value = entry.split("=", 1)
        if not name or name in result:
            deny("qa_duplicate_environment", 403)
        result[name] = value
    return result


class QADockerDriver(DockerDriver):
    def job(self, row):
        return self.name(row) + "-qa"

    def job_labels(self, row):
        return {**self.labels(row), "hogwarts.qa.role": "job"}

    def job_info(self, row):
        result = self.call("container", "inspect", self.job(row), allow_failure=True)
        if result.returncode:
            self.call("info", "--format", "{{.OSType}}")
            found = self.call(
                "container",
                "ls",
                "-a",
                "--filter",
                "name=^/" + self.job(row) + "$",
                "--format",
                "{{.Names}}",
            ).stdout.strip()
            if found:
                deny("qa_job_inspect_failed")
            return None
        info = json.loads(result.stdout)[0]
        if info["Config"].get("Labels") != self.job_labels(row):
            deny("qa_job_identity_conflict", 403)
        return info

    def verify_job(self, row, profile):
        holder = self.verify(row, profile)
        job = self.job_info(row)
        if job is None:
            deny("qa_job_missing")
        h, c = job["HostConfig"], job["Config"]
        # Compare every requested security control to the inherited, verified M2.2 holder.
        keys = (
            "NetworkMode",
            "ReadonlyRootfs",
            "Privileged",
            "Binds",
            "Devices",
            "DeviceRequests",
            "DeviceCgroupRules",
            "GroupAdd",
            "CgroupParent",
            "UsernsMode",
            "ExtraHosts",
            "PublishAllPorts",
            "VolumesFrom",
            "PortBindings",
            "CapAdd",
            "CapDrop",
            "SecurityOpt",
            "PidMode",
            "IpcMode",
            "UTSMode",
            "CgroupnsMode",
            "Memory",
            "MemorySwap",
            "NanoCpus",
            "PidsLimit",
            "Tmpfs",
            "Ulimits",
            "LogConfig",
            "ReadonlyPaths",
            "MaskedPaths",
        )
        mismatches = [k for k in keys if h.get(k) != holder["HostConfig"].get(k)]
        if (
            mismatches
            or job["Image"] != profile.image
            or c["User"] != "10001:10001"
            or c["Entrypoint"] != ["/usr/local/bin/python"]
            or c["Cmd"] != container_argv(row, profile)[1:]
            or environment_entries(c.get("Env")) != environment_entries(holder["Config"].get("Env"))
            or c.get("WorkingDir") != "/workspace"
            or c.get("Volumes")
            or not self.valid_mounts(row, job.get("Mounts"))
        ):
            deny(
                "qa_job_configuration_conflict"
                + ("_" + "_".join(mismatches) if mismatches else "_config_or_mount"),
                403,
            )
        return job

    def _create_job(self, row, profile):
        if self.seed_info(row) or self.job_info(row):
            deny("qa_job_partial_creation")
        self.verify(row, profile)
        args = [
            "container",
            "create",
            "--pull=never",
            "--name",
            self.job(row),
            "--user",
            "10001:10001",
            "--read-only",
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--ipc",
            "none",
            "--cgroupns",
            "private",
            "--cpus",
            str(profile.cpus),
            "--memory",
            str(profile.memory_mb) + "m",
            "--memory-swap",
            str(profile.memory_mb) + "m",
            "--pids-limit",
            str(profile.pids),
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--log-driver",
            "none",
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/output",
            "--env",
            "TMPDIR=/tmp",
            "--entrypoint",
            "/usr/local/bin/python",
        ]
        for k, v in self.job_labels(row).items():
            args += ["--label", k + "=" + v]
        for k, v in self.tmpfs().items():
            args += ["--tmpfs", k + ":" + v]
        args += [
            "--mount",
            "type=volume,src=" + self.volume(row) + ",dst=/workspace,readonly,volume-nocopy",
            profile.image,
            *container_argv(row, profile)[1:],
        ]
        identifier = self.call(*args).stdout.decode().strip()
        info = self.verify_job(row, profile)
        if info["Id"] != identifier:
            deny("qa_job_identity_changed")
        return identifier

    def _kill_job(self, row):
        info = self.job_info(row)
        if info and info["State"]["Running"]:
            # Exit may win the race after inspect. A failed kill is acceptable
            # ONLY when a fresh identity-checked observation confirms termination.
            self.call("container", "kill", self.job(row), allow_failure=True)
        info = self.job_info(row)
        if info is None or info["State"]["Running"]:
            deny("qa_job_stop_uncertain")

    def _attach(self, row, seconds):
        env = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "DOCKER_CONFIG": "/nonexistent",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "LC_ALL": "C",
        }
        process = subprocess.Popen(
            [self.executable, "container", "start", "-a", self.job(row)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
        )
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()

        def consume(stream, output):
            try:
                while chunk := stream.read(4096):
                    remaining = MAX_LOG - len(output)
                    output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()
                        break
            finally:
                stream.close()

        threads = [
            threading.Thread(target=consume, args=(process.stdout, buffers[0]), daemon=True),
            threading.Thread(target=consume, args=(process.stderr, buffers[1]), daemon=True),
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + seconds
        timed_out = False
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() >= deadline:
                timed_out = not overflow.is_set()
                self._kill_job(row)
                process.kill()
                break
            try:
                process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
        process.wait(timeout=3)
        for thread in threads:
            thread.join(3)
        if any(t.is_alive() for t in threads):
            deny("qa_attach_uncertain")
        return (
            bytes(buffers[0]),
            bytes(buffers[1]),
            timed_out,
            overflow.is_set(),
            process.returncode,
        )

    def execute(self, row, profile, seconds):
        identifier = self._create_job(row, profile)
        started = now().isoformat()
        # Independent image supervisor enforces its own deadline even if this process crashes.
        out, err, timed_out, overflow, cli_exit = self._attach(row, seconds + 2)
        # Attach EOF and Docker State publication can race. Wait observes only this
        # existing job; it never starts or replays a command.
        waited = self.call("container", "wait", self.job(row), timeout=3)
        info = self.verify_job(row, profile)
        state = info["State"]
        if waited.stdout.strip() != str(state["ExitCode"]).encode():
            deny("qa_exit_observation_mismatch")
        if info["Id"] != identifier or state["Running"] or state.get("Error"):
            deny("qa_execution_unknown")
        code = state["ExitCode"]
        oom = bool(state.get("OOMKilled"))
        start_time, finish = state.get("StartedAt"), state.get("FinishedAt")
        if (
            not start_time
            or start_time.startswith("0001-")
            or not finish
            or finish.startswith("0001-")
        ):
            deny("qa_missing_execution_time")
        # Supervisor timeout is a non-successful result; arbitrary stdout is never parsed.
        elapsed = (
            utc(datetime.fromisoformat(finish.replace("Z", "+00:00")))
            - utc(datetime.fromisoformat(start_time.replace("Z", "+00:00")))
        ).total_seconds()
        timed_out = timed_out or (
            code == 124 and elapsed >= min(seconds, profile.timeout_seconds) - 0.5
        )
        outcome = (
            "infrastructure_error"
            if overflow
            else "timeout"
            if timed_out
            else "fail"
            if oom
            else "unknown_execution"
            if code >= 128
            else "infrastructure_error"
            if cli_exit != code or code in {125, 126, 127}
            else "pass"
            if code == 0
            else "fail"
        )
        return ExecutionResult(
            outcome=outcome,
            exit_code=code,
            stdout=out.hex(),
            stderr=err.hex(),
            started_at=start_time if start_time else started,
            finished_at=finish,
            container_id=identifier,
            image_id=info["Image"],
            oom_killed=oom,
            timed_out=timed_out,
            error_code="qa_output_limit" if overflow else None,
        )

    def destroy(self, row):
        job = self.job_info(row)
        # Verify all identities BEFORE any removal, including partial startup resources.
        self.inspect(row)
        self.seed_info(row)
        self.volume_info(row)
        if job:
            self.call("container", "rm", "-f", self.job(row))
        super().destroy(row)
