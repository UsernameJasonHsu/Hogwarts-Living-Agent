"""Managed output volume and image-owned RO reader; default M2.2 driver is unchanged."""

import json
import os
import re
import selectors
import subprocess
import time

from hogwarts.outputs.contracts import MAX_ARCHIVE, deny
from hogwarts.sandboxes.controller import LinuxSandboxController
from hogwarts.sandboxes.driver import DockerDriver
from hogwarts.service import utc


class OutputController(LinuxSandboxController):
    def import_outputs(self, importer, actor, run_id, workspace_id, sandbox_id, lease_token):
        return importer.import_output(self, actor, run_id, workspace_id, sandbox_id, lease_token)


class OutputDockerDriver(DockerDriver):
    def __init__(self, controller_id, reader_image, executable=None):
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", reader_image):
            deny("unapproved_output_reader", 403)
        self.reader_image = reader_image
        super().__init__(controller_id, executable)
        info = json.loads(self.call("image", "inspect", reader_image).stdout)[0]
        if info["Os"] != "linux" or info["Config"].get("Volumes"):
            deny("unapproved_output_reader", 403)

    def output_volume(self, row):
        return self.name(row) + "-output"

    def reader_name(self, row):
        return self.name(row) + "-reader"

    def output_labels(self, row, role):
        return {**self.labels(row), "hogwarts.output_role": role}

    def output_options(self):
        return {
            "type": "tmpfs",
            "device": "tmpfs",
            "o": "size=16777216,nr_inodes=2048,uid=10001,gid=10001,mode=0700,nodev,nosuid,noexec",
        }

    def tmpfs(self):
        return {"/tmp": super().tmpfs()["/tmp"]}

    def call(self, *args, **kwargs):
        if args[:2] == ("container", "create") and "--name" in args:
            name = args[args.index("--name") + 1]
            if re.fullmatch(r"hogwarts-sb-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", name):
                args = (
                    *args[:2],
                    "--mount",
                    "type=volume,src=" + name + "-output,dst=/output,volume-nocopy",
                    *args[2:],
                )
        return super().call(*args, **kwargs)

    def valid_mounts(self, row, mounts):
        mounts = mounts or []
        input_mounts = [m for m in mounts if m["Destination"] == "/workspace"]
        output = [m for m in mounts if m["Destination"] == "/output"]
        return (
            len(mounts) == 2
            and super().valid_mounts(row, input_mounts)
            and len(output) == 1
            and output[0]["Type"] == "volume"
            and output[0]["Name"] == self.output_volume(row)
            and output[0]["RW"]
        )

    def output_info(self, row):
        result = self.call("volume", "inspect", self.output_volume(row), allow_failure=True)
        if result.returncode:
            self.call("info", "--format", "{{.OSType}}")
            names = self.call("volume", "ls", "--format", "{{.Name}}").stdout.decode().splitlines()
            if self.output_volume(row) in names:
                deny("output_volume_inspect_failed")
            return None
        info = json.loads(result.stdout)[0]
        if (
            info.get("Labels") != self.output_labels(row, "volume")
            or info["Driver"] != "local"
            or info.get("Options") != self.output_options()
        ):
            deny("output_volume_identity_conflict", 403)
        return info

    def reader_info(self, row):
        result = self.call("container", "inspect", self.reader_name(row), allow_failure=True)
        if result.returncode:
            self.call("info", "--format", "{{.OSType}}")
            names = (
                self.call("container", "ls", "-a", "--format", "{{.Names}}")
                .stdout.decode()
                .splitlines()
            )
            if self.reader_name(row) in names:
                deny("output_reader_inspect_failed")
            return None
        info = json.loads(result.stdout)[0]
        host, config = info["HostConfig"], info["Config"]
        mounts = info.get("Mounts", [])
        if (
            info["Image"] != self.reader_image
            or config.get("Labels") != self.output_labels(row, "reader")
            or config["User"] != "10001:10001"
            or config.get("Volumes")
            or config["Entrypoint"] != ["/usr/local/bin/python"]
            or config["Cmd"]
            != [
                "-I",
                "-B",
                "/opt/hogwarts-output-reader.py",
                "hold",
                str(int(utc(row.expires_at).timestamp())),
            ]
            or len(mounts) != 1
            or mounts[0]["Type"] != "volume"
            or mounts[0]["Name"] != self.output_volume(row)
            or mounts[0]["Destination"] != "/output"
            or mounts[0]["RW"]
            or not host["ReadonlyRootfs"]
            or host["NetworkMode"] != "none"
            or host["Privileged"]
            or host["CapDrop"] != ["ALL"]
            or host.get("CapAdd")
            or host["SecurityOpt"] != ["no-new-privileges"]
            or host["PidMode"]
            or host["IpcMode"] != "none"
            or host["UTSMode"]
            or host["CgroupnsMode"] != "private"
            or host.get("UsernsMode")
            or host.get("CgroupParent")
            or host.get("Binds")
            or host.get("Devices")
            or host.get("DeviceRequests")
            or host.get("DeviceCgroupRules")
            or host.get("GroupAdd")
            or host.get("VolumesFrom")
            or host.get("PortBindings")
            or host.get("PublishAllPorts")
            or host.get("ExtraHosts")
            or host.get("Tmpfs")
            or host["Memory"] != 64 * 1024 * 1024
            or host["MemorySwap"] != 64 * 1024 * 1024
            or host["NanoCpus"] != 250000000
            or host["PidsLimit"] != 8
        ):
            deny("output_reader_configuration_conflict", 403)
        return info

    def create(self, row, profile):
        if self.output_info(row) is not None or self.reader_info(row) is not None:
            deny("partial_output_allocation")
        args = ["volume", "create", "--driver", "local"]
        for key, value in self.output_labels(row, "volume").items():
            args += ["--label", key + "=" + value]
        for key, value in self.output_options().items():
            args += ["--opt", key + "=" + value]
        self.call(*args, self.output_volume(row))
        return super().create(row, profile)

    def verify(self, row, profile):
        info = super().verify(row, profile)
        if self.output_info(row) is None:
            deny("output_volume_missing")
        return info

    def start(self, row, profile, archive):
        if self.reader_info(row) is not None:
            deny("partial_output_reader")
        args = [
            "container",
            "create",
            "--pull=never",
            "--name",
            self.reader_name(row),
            "--user",
            "10001:10001",
            "--read-only",
            "--network",
            "none",
            "--ipc",
            "none",
            "--cgroupns",
            "private",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "0.25",
            "--memory",
            "64m",
            "--memory-swap",
            "64m",
            "--pids-limit",
            "8",
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--log-driver",
            "none",
            "--entrypoint",
            "/usr/local/bin/python",
            "--workdir",
            "/output",
            "--mount",
            "type=volume,src=" + self.output_volume(row) + ",dst=/output,readonly,volume-nocopy",
        ]
        for key, value in self.output_labels(row, "reader").items():
            args += ["--label", key + "=" + value]
        self.call(
            *args,
            self.reader_image,
            "-I",
            "-B",
            "/opt/hogwarts-output-reader.py",
            "hold",
            str(int(utc(row.expires_at).timestamp())),
        )
        self.call("container", "start", self.reader_name(row))
        self.reader_info(row)
        super().start(row, profile, archive)

    def _stopped(self, row, profile):
        producer = self.verify(row, profile)
        reader = self.reader_info(row)
        if (
            producer["State"]["Running"]
            or producer["State"]["Paused"]
            or not producer["State"].get("FinishedAt")
            or not reader
            or not reader["State"]["Running"]
            or reader["State"]["Paused"]
        ):
            deny("output_not_stable")
        attached = (
            self.call(
                "container",
                "ls",
                "-a",
                "--filter",
                "volume=" + self.output_volume(row),
                "--format",
                "{{.Names}}",
            )
            .stdout.decode()
            .splitlines()
        )
        if set(attached) != {self.name(row), self.reader_name(row)}:
            deny("output_has_unknown_mounts", 403)
        return {
            "container_id": producer["Id"],
            "reader_id": reader["Id"],
            "started_at": producer["State"]["StartedAt"],
            "finished_at": producer["State"]["FinishedAt"],
            "output_volume": self.output_volume(row),
            "reader_image": self.reader_image,
            "invocation_id": row.invocation_id,
            "profile_hash": row.request_hash,
            "sandbox_input_sha256": row.snapshot_sha256,
        }

    def _stream(self, row):
        env = {
            "PATH": os.defpath,
            "HOME": "/nonexistent",
            "DOCKER_CONFIG": "/nonexistent",
            "LC_ALL": "C",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
        }
        args = [
            self.executable,
            "container",
            "exec",
            "--user",
            "10001:10001",
            self.reader_name(row),
            "/usr/local/bin/python",
            "-I",
            "-B",
            "/opt/hogwarts-output-reader.py",
            "archive",
        ]
        stdout, stderr = bytearray(), bytearray()
        deadline = time.monotonic() + 10
        with subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, shell=False
        ) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, stdout)
                    selector.register(process.stderr, selectors.EVENT_READ, stderr)
                    while selector.get_map():
                        if time.monotonic() > deadline:
                            deny("output_capture_timeout")
                        for key, _ in selector.select(0.1):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                            else:
                                key.data.extend(data)
                                if len(stdout) > MAX_ARCHIVE - 16384 or len(stderr) > 8192:
                                    deny("output_capture_limit")
                if process.wait(timeout=max(0.1, deadline - time.monotonic())):
                    deny("output_capture_failed")
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                deny("output_capture_timeout")
            except BaseException:
                process.kill()
                process.wait()
                raise
        return bytes(stdout)

    def export_output(self, row, profile):
        before = self._stopped(row, profile)
        data = self._stream(row)
        if self._stopped(row, profile) != before:
            deny("output_changed_during_capture")
        return data, before

    def destroy(self, row):
        # Verify all output identities BEFORE removing any resource.
        volume, reader = self.output_info(row), self.reader_info(row)
        super().destroy(row)
        if reader:
            self.call("container", "rm", "-f", self.reader_name(row))
        if volume:
            self.call("volume", "rm", self.output_volume(row))
