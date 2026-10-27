"""Bounded logical accounting. No byte reads, deletions or physical-quota claim."""

import os
import shutil
import stat
from pathlib import Path
from types import MappingProxyType

from sqlalchemy import select

from hogwarts.models import ArtifactMetadata
from hogwarts.operations.contracts import StorageUsage
from hogwarts.reconciliation.contracts import deny
from hogwarts.service import digest
from hogwarts.workspaces.git import safe_directory


class StorageUsageAccounting:
    def __init__(
        self,
        roots,
        *,
        min_free_bytes=64 * 1024 * 1024,
        max_files=20000,
        max_bytes=2**40,
        disk_usage=shutil.disk_usage,
        excluded=(),
    ):
        if type(min_free_bytes) is not int or min_free_bytes < 0:
            deny("storage_invalid_configuration", 403)
        if type(max_files) is not int or not 1 <= max_files <= 100000:
            deny("storage_invalid_configuration", 403)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 2**50:
            deny("storage_invalid_configuration", 403)
        configured = {}
        seen = []
        for owner, paths in roots.items():
            if not paths or len(paths) > 16 or len(roots) > 32:
                deny("storage_roots_required", 403)
            configured[owner] = tuple(Path(p) for p in paths)
            for p in configured[owner]:
                if not p.is_absolute() or p.parent == p or safe_directory(p) != p:
                    deny("storage_unsafe_root", 403)
                if any(p == q or p in q.parents or q in p.parents for q in (*seen, *excluded)):
                    deny("storage_overlapping_root", 403)
                seen.append(p)
        self.roots = MappingProxyType(configured)
        self.min_free_bytes, self.max_files, self.max_bytes = min_free_bytes, max_files, max_bytes
        self.disk_usage = disk_usage

    @staticmethod
    def identity(st):
        return [
            st.st_dev,
            st.st_ino,
            st.st_mode,
            st.st_nlink,
            st.st_size,
            st.st_mtime_ns,
            st.st_ctime_ns,
        ]

    def tree(self, root):
        result, total = {}, 0
        stack = [(root, "")]
        while stack:
            path, name = stack.pop()
            st = path.lstat()
            if (
                stat.S_ISLNK(st.st_mode)
                or getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                deny("storage_unsafe_entry")
            if stat.S_ISDIR(st.st_mode):
                safe_directory(path)
                result[name] = self.identity(st)
                with os.scandir(path) as entries:
                    for entry in entries:
                        if len(result) + len(stack) >= self.max_files:
                            deny("storage_inventory_limit")
                        stack.append((path / entry.name, name + "/" + entry.name))
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                total += st.st_size
                if total > self.max_bytes:
                    deny("storage_inventory_limit")
                result[name] = self.identity(st)
            else:
                deny("storage_unsafe_entry")
        return result, total

    def measure(self, s, owner):
        roots = self.roots.get(owner)
        if not roots:
            deny("storage_owner_not_configured", 403)
        scans, total, count, free = [], 0, 0, []
        for p in roots:
            safe_directory(p)
            first, size = self.tree(p)
            second, _ = self.tree(p)
            if first != second:
                deny("storage_tree_changed")
            scans.append(first)
            total += size
            count += sum(stat.S_ISREG(x[2]) for x in first.values())
            free.append(self.disk_usage(p).free)
            if os.name == "posix":
                fs = os.statvfs(p)
                if fs.f_files and fs.f_favail == 0:
                    deny("storage_inodes_exhausted")
        if total > self.max_bytes or count > self.max_files:
            deny("storage_inventory_limit")
        metadata = list(
            s.scalars(
                select(ArtifactMetadata)
                .where(ArtifactMetadata.owner_id == owner)
                .limit(self.max_files + 1)
            )
        )
        if len(metadata) > self.max_files:
            deny("storage_inventory_limit")
        missing = 0
        for row in metadata:
            if row.state in {"FAILED", "CORRUPT"}:
                deny("storage_artifact_state_uncertain")
            sizes = [
                info[4]
                for tree in scans
                for name, info in tree.items()
                if name.endswith("/" + row.id + ".blob") and stat.S_ISREG(info[2])
            ]
            if len(sizes) > 1:
                deny("storage_ambiguous_artifact")
            missing += max(0, row.size_bytes - (sizes[0] if sizes else 0))
        return StorageUsage(
            logical_bytes=total,
            declared_artifact_bytes=sum(x.size_bytes for x in metadata),
            unmaterialized_artifact_bytes=missing,
            incomplete_artifact_count=sum(x.state != "READY" for x in metadata),
            artifact_count=len(metadata),
            file_count=count,
            free_bytes_min=min(free),
            fingerprint=digest(scans),
        )
