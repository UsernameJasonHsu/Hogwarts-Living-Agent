"""Trusted daemon client. Fixed argv; never pass agent flags, env or host mounts."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from hogwarts.sandboxes.contracts import deny
from hogwarts.service import utc


class DockerDriver:
    def __init__(self, controller_id, executable=None):
        if sys.platform != "linux":
            deny("linux_controller_required", 403)
        if not re.fullmatch(r"[a-z0-9-]{1,40}", controller_id):
            deny("invalid_controller_id", 403)
        self.controller_id = controller_id
        self.executable = str(Path(executable or shutil.which("docker") or "/missing").resolve())
        if not Path(self.executable).is_file():
            deny("docker_unavailable")
        info = json.loads(self.call("info", "--format", "{{json .}}").stdout)
        if (
            info.get("OSType") != "linux"
            or info.get("CgroupVersion") != "2"
            or not all(info.get(k) for k in ("MemoryLimit", "PidsLimit", "CpuCfsQuota"))
            or not any("seccomp" in str(o) for o in info.get("SecurityOptions", []))
        ):
            deny("unsupported_docker_security")

    def call(self, *args, data=None, timeout=20, allow_failure=False):
        env = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "DOCKER_CONFIG": "/nonexistent",
            "LC_ALL": "C",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        }
        try:
            result = subprocess.run(
                [self.executable, *args],
                input=data,
                capture_output=True,
                timeout=timeout,
                env=env,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            deny("sandbox_timeout")
        except OSError:
            deny("docker_unavailable")
        if len(result.stdout) > 65536 or len(result.stderr) > 65536:
            deny("runtime_output_limit")
        if result.returncode and not allow_failure:
            deny("docker_operation_failed")
        return result

    def labels(self, row):
        return {
            "hogwarts.controller": self.controller_id,
            "hogwarts.sandbox": row.id,
            "hogwarts.run": row.run_id,
            "hogwarts.workspace": row.workspace_id,
            "hogwarts.profile": row.request_hash,
        }

    def name(self, row):
        from uuid import UUID

        try:
            if str(UUID(row.id)) != row.id:
                deny("invalid_sandbox_id", 403)
        except ValueError:
            deny("invalid_sandbox_id", 403)
        return "hogwarts-sb-" + row.id

    def inspect(self, row):
        result = self.call("container", "inspect", self.name(row), allow_failure=True)
        if result.returncode:
            # Distinguish daemon/permission failures from a genuinely absent owned name.
            self.call("info", "--format", "{{.OSType}}")
            listed = json.loads(
                self.call(
                    "container",
                    "ls",
                    "-a",
                    "--filter",
                    "name=^/" + self.name(row) + "$",
                    "--format",
                    "json",
                ).stdout
                or b"{}"
            )
            if listed:
                deny("sandbox_inspect_failed")
            return None
        info = json.loads(result.stdout)[0]
        if info["Config"].get("Labels") != self.labels(row) or (
            row.container_id and info["Id"] != row.container_id
        ):
            deny("sandbox_identity_conflict", 403)
        return info

    def verify(self, row, profile):
        info = self.inspect(row)
        if info is None:
            deny("sandbox_missing")
        host, config = info["HostConfig"], info["Config"]
        if (
            info["Image"] != profile.image
            or config["User"] != "10001:10001"
            or config["Entrypoint"] != ["/usr/local/bin/python"]
            or config["Cmd"]
            != [
                "-I",
                "-B",
                "/opt/hogwarts/runner.py",
                "--idle",
                str(int(utc(row.expires_at).timestamp())),
            ]
            or host["NetworkMode"] != "none"
            or not host["ReadonlyRootfs"]
            or host["Privileged"]
            or host.get("Binds")
            or host.get("Devices")
            or host.get("DeviceRequests")
            or host.get("DeviceCgroupRules")
            or host.get("GroupAdd")
            or host.get("CgroupParent")
            or host.get("UsernsMode")
            or host.get("ExtraHosts")
            or host.get("PublishAllPorts")
            or host.get("VolumesFrom")
            or host.get("PortBindings")
            or host.get("CapAdd")
            or host["CapDrop"] != ["ALL"]
            or host["SecurityOpt"] != ["no-new-privileges"]
            or host["PidMode"]
            or host["IpcMode"] != "none"
            or host["UTSMode"]
            or host["CgroupnsMode"] != "private"
            or host["Memory"] != profile.memory_mb * 1024 * 1024
            or host["MemorySwap"] != host["Memory"]
            or host["NanoCpus"] != int(profile.cpus * 1_000_000_000)
            or host["PidsLimit"] != profile.pids
            or host["Tmpfs"] != self.tmpfs()
            or not self.valid_mounts(row, info.get("Mounts"))
            or config.get("Volumes")
        ):
            deny("sandbox_configuration_conflict", 403)
        self.volume_info(row)
        return info

    def tmpfs(self):
        return {
            "/output": "rw,noexec,nosuid,nodev,size=16777216,nr_inodes=2048,"
            "uid=10001,gid=10001,mode=0700",
            "/tmp": "rw,noexec,nosuid,nodev,size=8388608,nr_inodes=1024,"
            "uid=10001,gid=10001,mode=0700",
        }

    def volume_options(self):
        return {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": "size=67108864,nr_inodes=12000,uid=10001,gid=10001,mode=0700,nodev,nosuid,noexec",
        }

    def volume(self, row):
        return self.name(row) + "-input"

    def valid_mounts(self, row, mounts):
        return (
            len(mounts or []) == 1
            and mounts[0]["Type"] == "volume"
            and mounts[0]["Name"] == self.volume(row)
            and mounts[0]["Destination"] == "/workspace"
            and not mounts[0]["RW"]
        )

    def volume_info(self, row):
        result = self.call("volume", "inspect", self.volume(row), allow_failure=True)
        if result.returncode:
            self.call("info", "--format", "{{.OSType}}")
            names = self.call("volume", "ls", "--format", "{{.Name}}").stdout.decode().splitlines()
            if self.volume(row) in names:
                deny("sandbox_volume_inspect_failed")
            return None
        info = json.loads(result.stdout)[0]
        if (
            info.get("Labels") != self.labels(row)
            or info["Driver"] != "local"
            or info.get("Options") != self.volume_options()
        ):
            deny("sandbox_volume_identity_conflict", 403)
        return info

    def seed_info(self, row):
        result = self.call("container", "inspect", self.name(row) + "-seed", allow_failure=True)
        if result.returncode:
            self.call("info", "--format", "{{.OSType}}")
            names = (
                self.call("container", "ls", "-a", "--format", "{{.Names}}")
                .stdout.decode()
                .splitlines()
            )
            if self.name(row) + "-seed" in names:
                deny("sandbox_seed_inspect_failed")
            return None
        info = json.loads(result.stdout)[0]
        if info["Config"].get("Labels") != self.labels(row):
            deny("sandbox_seed_identity_conflict", 403)
        return info

    def create(self, row, profile):
        if self.inspect(row) is not None:
            deny("sandbox_partial_creation")
        image = json.loads(self.call("image", "inspect", profile.image).stdout)[0]
        if image["Os"] != "linux" or image["Config"].get("Volumes"):
            deny("unapproved_runtime_image", 403)
        if self.volume_info(row) is not None:
            deny("sandbox_partial_creation")
        volume_args = ["volume", "create", "--driver", "local"]
        for key, value in self.labels(row).items():
            volume_args += ["--label", key + "=" + value]
        for key, value in self.volume_options().items():
            volume_args += ["--opt", key + "=" + value]
        self.call(*volume_args, self.volume(row))
        args = [
            "container",
            "create",
            "--pull=never",
            "--name",
            self.name(row),
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
        # Explicit entrypoint overrides the approved image entrypoint, including its args.
        for key, value in self.labels(row).items():
            args += ["--label", key + "=" + value]
        for key, value in self.tmpfs().items():
            args += ["--tmpfs", key + ":" + value]
        args += [
            "--mount",
            "type=volume,src=" + self.volume(row) + ",dst=/workspace,readonly,volume-nocopy",
        ]
        args += [
            profile.image,
            "-I",
            "-B",
            "/opt/hogwarts/runner.py",
            "--idle",
            str(int(utc(row.expires_at).timestamp())),
        ]
        result = self.call(*args)
        return result.stdout.decode().strip()

    def start(self, row, profile, archive):
        self.verify(row, profile)
        self.volume_info(row)
        if self.seed_info(row) is not None:
            deny("sandbox_partial_initialization")
        # The trusted image-owned seeder is also non-root and never runs source code.
        args = [
            "container",
            "create",
            "-i",
            "--pull=never",
            "--name",
            self.name(row) + "-seed",
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
            "128m",
            "--memory-swap",
            "128m",
            "--pids-limit",
            "8",
            "--log-driver",
            "none",
            "--entrypoint",
            "/usr/local/bin/python",
            "--mount",
            "type=volume,src=" + self.volume(row) + ",dst=/workspace,volume-nocopy",
        ]
        for key, value in self.labels(row).items():
            args += ["--label", key + "=" + value]
        args += [
            profile.image,
            "-I",
            "-B",
            "/opt/hogwarts/runner.py",
            "--seed",
            str(int(utc(row.expires_at).timestamp())),
        ]
        self.call(*args)
        self.call("container", "start", self.name(row))
        self.call("container", "start", "-a", "-i", self.name(row) + "-seed", data=archive)
        self.call("container", "rm", self.name(row) + "-seed")
        self.verify(row, profile)

    def execute(self, row, profile, seconds):
        self.verify(row, profile)
        result = self.call(
            "container",
            "exec",
            "--user",
            "10001:10001",
            self.name(row),
            "/usr/local/bin/python",
            "-I",
            "-B",
            "/opt/hogwarts/runner.py",
            profile.command,
            timeout=seconds,
            allow_failure=True,
        )
        try:
            payload = json.loads(result.stdout) if result.stdout else {}
        except ValueError:
            deny("invalid_runtime_result")
        return {"exit_code": result.returncode, "output": payload}

    def stop(self, row):
        seed = self.seed_info(row)
        if seed and seed["State"]["Running"]:
            self.call("container", "kill", self.name(row) + "-seed")
        info = self.inspect(row)
        if info and info["State"]["Running"]:
            self.call("container", "kill", self.name(row))

    def destroy(self, row):
        # Verify every identity before deleting any resource. Never blindly remove by prefix.
        info, seed, volume = self.inspect(row), self.seed_info(row), self.volume_info(row)
        if info:
            self.call("container", "rm", "-f", self.name(row))
        if seed:
            self.call("container", "rm", "-f", self.name(row) + "-seed")
        if volume:
            self.call("volume", "rm", self.volume(row))
