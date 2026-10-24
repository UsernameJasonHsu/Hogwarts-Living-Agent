"""Trusted image-owned readonly exporter; never execute source/output programs."""

import io
import os
import signal
import stat
import sys
import tarfile
import time
from pathlib import Path


def snapshot():
    signal.alarm(8)
    result = io.BytesIO()
    count = files = total = 0
    with tarfile.open(fileobj=result, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        stack = [Path("/output")]
        while stack:
            folder = stack.pop()
            for path in sorted(folder.iterdir()):
                count += 1
                info = path.lstat()
                if count > 512 or stat.S_ISLNK(info.st_mode):
                    raise ValueError("unsafe output entry")
                name = path.relative_to("/output").as_posix()
                entry = tarfile.TarInfo(name)
                if stat.S_ISDIR(info.st_mode):
                    entry.type, entry.mode = tarfile.DIRTYPE, 0o755
                    stack.append(path)
                    archive.addfile(entry)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    files += 1
                    total += info.st_size
                    if files > 256 or info.st_size > 4 * 1024 * 1024 or total > 8 * 1024 * 1024:
                        raise ValueError("output exceeds limits")
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as stream:
                        opened = os.fstat(stream.fileno())
                        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
                            info.st_dev,
                            info.st_ino,
                            1,
                        ):
                            raise ValueError("changed output identity")
                        content = stream.read(info.st_size + 1)
                        after = os.fstat(stream.fileno())
                        if len(content) != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
                            raise ValueError("changed output")
                    entry.mode, entry.size = 0o644, len(content)
                    archive.addfile(entry, io.BytesIO(content))
                else:
                    raise ValueError("links or special files forbidden")
    data = result.getvalue()
    if len(data) > 12 * 1024 * 1024 - 16384:
        raise ValueError("archive limit")
    return data


if __name__ == "__main__":
    if sys.argv[1] == "hold":
        time.sleep(max(0, float(sys.argv[2]) - time.time()))
    elif sys.argv[1] == "archive":
        sys.stdout.buffer.write(snapshot())
    else:
        raise ValueError("unsupported fixed reader command")
