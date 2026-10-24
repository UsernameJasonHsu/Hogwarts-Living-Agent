"""Image-owned deterministic probes; no repository scripts or supplied shell evaluated."""

import errno
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path


def denied_write(path):
    try:
        with open(path, "wb") as stream:
            stream.write(b"forbidden")
    except OSError as error:
        return error.errno in {errno.EACCES, errno.EROFS, errno.ENOENT}
    return False


def unavailable(path):
    try:
        return not Path(path).exists()
    except PermissionError:
        return True


def main(command):
    if command == "--seed":
        remaining = int(float(sys.argv[2]) - time.time())
        if remaining <= 0:
            raise ValueError("expired input initialization")
        signal.alarm(min(20, remaining))
        # Controller supplies bounded tar; independently reject links/traversal/duplicates.
        directories, count, total = set(), 0, 0
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|*") as stream:
            for item in stream:
                count += 1
                parts = item.name.split("/")
                if (
                    count > 12000
                    or any(p in {"", ".", "..", ".git"} for p in parts)
                    or not (item.isdir() or item.isreg())
                    or item.size > 8 * 1024 * 1024
                ):
                    raise ValueError("unsafe input archive")
                path = Path("/workspace").joinpath(*parts)
                if item.isdir():
                    path.mkdir(mode=0o755)
                    directories.add(path)
                else:
                    total += item.size
                    if total > 48 * 1024 * 1024:
                        raise ValueError("input too large")
                    with path.open("xb") as output:
                        output.write(stream.extractfile(item).read())
                    path.chmod(0o444)
        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            directory.chmod(0o555)
        return {}
    if command == "--idle":
        time.sleep(max(0, float(sys.argv[2]) - time.time()))
        return {}
    # One execution per container, even if a caller retries after losing its DB transaction.
    with open("/output/.executed", "x"):
        pass
    if command == "verify":
        status = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        )
        connection = socket.socket()
        connection.settimeout(0.3)
        try:
            connection.connect(("1.1.1.1", 443))
            blocked = False
        except OSError:
            blocked = True
        finally:
            connection.close()
        try:
            os.setuid(0)
            root_denied = False
        except PermissionError:
            root_denied = True
        Path("/output/prototype.txt").write_text("deterministic prototype\n")
        Path("/tmp/scratch").write_text("scratch")
        return {
            "root_escalation_denied": root_denied,
            "uid": os.getuid(),
            "gid": os.getgid(),
            "cap_eff": status["CapEff"].strip(),
            "no_new_privs": status["NoNewPrivs"].strip(),
            "seccomp": status["Seccomp"].strip(),
            "network_blocked": blocked,
            "interfaces": sorted(p.name for p in Path("/sys/class/net").iterdir()),
            "readonly_shm": denied_write("/dev/shm/probe"),
            "readonly_root": denied_write("/opt/hogwarts/probe"),
            "readonly_workspace": denied_write("/workspace/probe"),
            "git_absent": not Path("/workspace/.git").exists(),
            "host_secrets_unavailable": all(
                unavailable(p)
                for p in (
                    "/var/run/docker.sock",
                    "/run/secrets",
                    "/root/.ssh",
                    "/host",
                    "/host-sentinel",
                )
            ),
            "environment": sorted(os.environ),
            "input_files": sorted(
                str(p.relative_to("/workspace"))
                for p in Path("/workspace").rglob("*")
                if p.is_file()
            ),
        }
    if command == "timeout":
        subprocess.Popen(["/usr/local/bin/python", "-I", "-c", "import time; time.sleep(120)"])
        time.sleep(120)
    if command == "fail":
        return {"intentional_failure": True}
    if command == "memory":
        # RLIMIT_AS would mask cgroup enforcement; allocate/touch beyond memory.max.
        data = bytearray(768 * 1024 * 1024)
        return {"unexpected_allocation": len(data)}
    if command == "pids":
        children = []
        try:
            while True:
                children.append(
                    subprocess.Popen(
                        ["/usr/local/bin/python", "-I", "-c", "import time; time.sleep(30)"]
                    )
                )
        except OSError as error:
            result = {"pids_denied": error.errno == errno.EAGAIN, "children": len(children)}
        finally:
            for child in children:
                child.kill()
            for child in children:
                child.wait()
        return result
    if command == "disk":
        try:
            with open("/output/fill", "wb") as stream:
                for _ in range(64):
                    stream.write(b"x" * 1024 * 1024)
        except OSError as error:
            return {"disk_denied": error.errno == errno.ENOSPC}
        return {"disk_denied": False}
    if command == "cpu":
        before = Path("/sys/fs/cgroup/cpu.stat").read_text()
        end = time.monotonic() + 2
        while time.monotonic() < end:
            pass
        return {
            "cpu_max": Path("/sys/fs/cgroup/cpu.max").read_text().strip(),
            "before": before,
            "after": Path("/sys/fs/cgroup/cpu.stat").read_text(),
        }
    raise ValueError("unapproved profile")


if __name__ == "__main__":
    answer = main(sys.argv[1])
    print(json.dumps(answer, sort_keys=True))
    if sys.argv[1] == "fail":
        sys.exit(7)
