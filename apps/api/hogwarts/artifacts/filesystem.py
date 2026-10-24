"""Private immutable objects; no caller filenames or path-based imports."""

import hashlib
import os
import stat
from pathlib import Path
from uuid import UUID

from hogwarts.artifacts.contracts import MAX_BYTES, IntegrityError, reject
from hogwarts.workspaces.git import safe_directory


class LocalObjects:
    def __init__(self, root: Path, excluded: tuple[Path, ...]):
        if not Path(root).is_absolute():
            reject("artifact_root_not_absolute", 403)
        candidate = safe_directory(root)
        if candidate.parent == candidate:
            reject("unsafe_artifact_root", 403)
        for directory in excluded:
            other = safe_directory(directory)
            if candidate == other or candidate in other.parents or other in candidate.parents:
                reject("unsafe_artifact_root", 403)
        # The root and objects child belong only to this store, never a worktree/container.
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = candidate
        self.objects = candidate / "objects"
        self.objects.mkdir(mode=0o700, exist_ok=True)
        self.check_root()

    def check_root(self):
        for path in (self.root, self.objects):
            safe_directory(path)
            info = path.stat()
            if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077):
                reject("artifact_root_not_private", 403)

    def path(self, artifact_id):
        try:
            if not isinstance(artifact_id, str) or str(UUID(artifact_id)) != artifact_id:
                reject("invalid_artifact_id", 403)
        except (ValueError, AttributeError):
            reject("invalid_artifact_id", 403)
        self.check_root()
        return self.objects / (artifact_id + ".blob")

    def _regular(self, info):
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise IntegrityError(409, "unsafe_artifact_object")

    def _check_fd(self, path, descriptor):
        before, opened = path.lstat(), os.fstat(descriptor)
        self._regular(before)
        self._regular(opened)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise IntegrityError(409, "artifact_object_changed")
        return opened

    def read(self, artifact_id, sha256, size):
        if not isinstance(size, int) or not 0 <= size <= MAX_BYTES:
            raise IntegrityError(409, "artifact_size_invalid")
        path = self.path(artifact_id)
        try:
            self._regular(path.lstat())
            descriptor = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            raise IntegrityError(409, "artifact_missing") from None
        with os.fdopen(descriptor, "rb") as stream:
            opened = self._check_fd(path, stream.fileno())
            if opened.st_size != size:
                raise IntegrityError(409, "artifact_integrity_failed")
            data = stream.read(size + 1)
            after = self._check_fd(path, stream.fileno())
            if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise IntegrityError(409, "artifact_object_changed")
        self.check_root()
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise IntegrityError(409, "artifact_integrity_failed")
        return data

    def publish(self, artifact_id, content, sha256):
        path = self.path(artifact_id)
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            # Exact-request recovery only; never replace a file or a link.
            self.read(artifact_id, sha256, len(content))
            # Crash may have occurred before fsync/chmod. Re-establish durability
            # without writing any byte; Windows FlushFileBuffers needs write access.
            if os.name == "nt":
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
            flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
            recovered = os.open(
                path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            )
            try:
                self._check_fd(path, recovered)
                os.fsync(recovered)
                if os.name == "posix":
                    os.fchmod(recovered, 0o400)
            finally:
                os.close(recovered)
                if os.name == "nt":
                    path.chmod(stat.S_IREAD)
            if os.name == "posix":
                directory = os.open(self.objects, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            self.read(artifact_id, sha256, len(content))
            return
        with os.fdopen(descriptor, "wb") as stream:
            self._check_fd(path, stream.fileno())
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            self._check_fd(path, stream.fileno())
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o400)
            else:
                path.chmod(stat.S_IREAD)
        if os.name == "posix":
            directory = os.open(self.objects, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        self.read(artifact_id, sha256, len(content))
