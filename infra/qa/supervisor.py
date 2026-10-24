"""Image-owned PID1: fixed argv, absolute expiry, bounded child and signal cleanup."""

import errno
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def probe(name):
    if name == "timeout":
        time.sleep(120)
    elif name == "oom":
        blocks = []
        while True:
            blocks.append(bytearray(8 * 1024 * 1024))
    elif name == "pids":
        children = []
        try:
            for _ in range(128):
                try:
                    child = os.fork()
                except OSError as error:
                    assert error.errno == errno.EAGAIN
                    print("pid limit enforced", flush=True)
                    return 0
                if child == 0:
                    time.sleep(120)
                    os._exit(0)
                children.append(child)
            return 1
        finally:
            for child in children:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
    elif name == "isolation":
        status = dict(
            line.split(":", 1)
            for line in Path("/proc/self/status").read_text().splitlines()
            if ":" in line
        )
        assert os.getuid() == os.getgid() == 10001
        assert int(status["CapEff"].strip(), 16) == 0 and status["NoNewPrivs"].strip() == "1"
        assert status["Seccomp"].strip() == "2"
        assert sorted(p.name for p in Path("/sys/class/net").iterdir()) == ["lo"]
        assert float(Path("/sys/fs/cgroup/memory.max").read_text()) <= 512 * 1024 * 1024
        assert int(Path("/sys/fs/cgroup/pids.max").read_text()) <= 64
        assert Path("/sys/fs/cgroup/cpu.max").read_text().split()[0] != "max"
        for path in ("/var/run/docker.sock", "/root/.ssh", "/root/.aws", "/host", "/run/secrets"):
            try:
                assert not Path(path).exists()
            except PermissionError:
                pass
        for path in ("/outside", "/workspace/outside", "/opt/hogwarts/qa-supervisor.py"):
            try:
                Path(path).write_text("forbidden")
            except OSError:
                pass
            else:
                raise AssertionError("unexpected writable path")
        assert not any(
            k.startswith(("AWS_", "SSH_", "TEST_DATABASE", "DOCKER_", "HOGWARTS_"))
            for k in os.environ
        )
        connection = socket.socket()
        connection.settimeout(0.3)
        try:
            connection.connect(("1.1.1.1", 443))
        except OSError:
            pass
        else:
            raise AssertionError("unexpected network")
        finally:
            connection.close()
        Path("/tmp/qa-write").write_text("bounded scratch")
        print("isolation enforced", flush=True)
    else:
        raise ValueError("unapproved probe")
    return 0


def main():
    if sys.argv[1] == "--probe":
        return probe(sys.argv[2])
    if sys.argv[1] != "--run" or len(sys.argv) != 6:
        return 125
    command, root, timeout, expiry = sys.argv[2:]
    remaining = min(int(timeout), int(expiry) - time.time())
    if remaining <= 0:
        return 124
    # Image supervisor is immutable. Source code runs only in the child process.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(125))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(125))
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(124))
    signal.alarm(max(1, int(remaining) + 1))
    if command == "oom":
        # OOM probe kills PID1 so Docker's authoritative OOMKilled bit is observable.
        return probe(command)
    if command == "unittest":
        argv = [
            "/usr/local/bin/python",
            "-I",
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "/workspace/" + root,
            "-p",
            "test_*.py",
            "-v",
        ]
    elif command in {"timeout", "pids", "isolation"}:
        argv = ["/usr/local/bin/python", "-I", "-B", __file__, "--probe", command]
    else:
        return 125
    child = subprocess.Popen(argv, start_new_session=True, shell=False)
    try:
        try:
            result = child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            return 124
        return result if result >= 0 else 128 - result
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


if __name__ == "__main__":
    sys.exit(main())
