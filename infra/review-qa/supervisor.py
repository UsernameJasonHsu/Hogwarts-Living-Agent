"""Pinned image owns the command; workload content never enters this interpreter."""

import os
import signal
import subprocess
import sys
import time


def main():
    if len(sys.argv) != 6 or sys.argv[1:3] != ["--run", "acceptance"]:
        return 125
    root, timeout, expiry = sys.argv[3:]
    remaining = min(int(timeout), int(expiry) - time.time())
    if remaining <= 0:
        return 124
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(125))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(125))
    signal.signal(signal.SIGALRM, lambda *_: sys.exit(124))
    signal.alarm(max(1, int(remaining) + 1))
    child = subprocess.Popen(
        ["/usr/local/bin/python", "-I", "-B", "/opt/hogwarts/review-tests.py", root],
        start_new_session=True,
        shell=False,
    )
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
