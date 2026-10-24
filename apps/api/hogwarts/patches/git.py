"""Raw Git objects/indexes only; no source worktree writes, checkout, filters or code."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from hogwarts.artifacts.filesystem import LocalObjects
from hogwarts.patches.contracts import MAX_PATCH, BuildProof, deny
from hogwarts.workspaces.git import (
    LocalGit,
    plain,
    relative_path,
    safe_directory,
    unambiguous,
    walk_files,
    within,
)


def blob_id(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class PrivateGit:
    def __init__(self, root, source_git):
        self.root = root
        self.source_git = source_git
        self.config = LocalGit(root, source_git.executable)
        self.version = self.invoke(None, "--version").decode("ascii").strip()

    def invoke(self, scratch, *args, data=None, index=None):
        command = [
            str(self.config.executable),
            "--no-pager",
            "-c",
            "core.hooksPath=" + str(self.config.hooks),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.protectNTFS=true",
            "-c",
            "core.quotePath=true",
            "-c",
            "core.attributesFile=" + str(self.config.empty),
            "-c",
            "protocol.allow=never",
            "-c",
            "submodule.recurse=false",
        ]
        if scratch is not None:
            safe_directory(scratch)
            command += ["--git-dir=" + str(scratch / "git"), "--work-tree=" + str(scratch / "work")]
        # Private files avoid buffering unbounded subprocess output in RAM.
        with (
            tempfile.TemporaryFile(dir=self.root) as out,
            tempfile.TemporaryFile(dir=self.root) as err,
        ):
            try:
                process = subprocess.run(
                    [*command, *args],
                    input=data,
                    stdout=out,
                    stderr=err,
                    env=self.config.environment(index),
                    shell=False,
                    timeout=30,
                    check=False,
                    cwd=scratch or self.root,
                )
            except subprocess.TimeoutExpired:
                deny("patch_git_timeout")
            if process.returncode:
                deny("patch_git_command_failed")
            if out.tell() > MAX_PATCH or err.tell() > 65536:
                deny("patch_git_output_limit")
            out.seek(0)
            return out.read(MAX_PATCH + 1)

    def _index(self, scratch, entries, index=None):
        data = b"".join(
            (mode + " blob " + sha + "\t" + path).encode("utf-8") + b"\0"
            for path, (mode, sha) in sorted(entries.items())
        )
        self.invoke(scratch, "update-index", "-z", "--index-info", data=data, index=index)

    def _entries(self, scratch, index=None):
        result = {}
        for record in self.invoke(scratch, "ls-files", "--stage", "-z", index=index).split(b"\0"):
            if not record:
                continue
            meta, name = record.split(b"\t", 1)
            mode, sha, stage = meta.decode().split()
            path = relative_path(name.decode("utf-8"))
            if stage != "0" or mode not in {"100644", "100755"} or path in result:
                deny("patch_unsafe_index")
            result[path] = (mode, sha)
        return result

    def snapshot(self, work, expected):
        found = walk_files(work)
        if set(found) != set(expected):
            deny("patch_scratch_file_set_changed")
        for name, data in expected.items():
            path = work / name
            before = plain(path)
            fd = os.open(
                path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                LocalObjects._regular(None, opened)
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    deny("patch_scratch_identity_changed")
                actual = stream.read(len(data) + 1)
                after = os.fstat(stream.fileno())
                current = plain(path)
                if (
                    (opened.st_dev, opened.st_ino, opened.st_mtime_ns, opened.st_size)
                    != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)
                    or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
                    or after.st_nlink != 1
                    or actual != data
                ):
                    deny("patch_scratch_content_changed")
        return True

    def _construct(self, scratch, inputs):
        c = inputs.context
        base = self.source_git.tree(c.repository, c.base_commit)
        if len(base) > 10000:
            deny("patch_base_path_limit")
        commit_size = int(self.source_git.run(c.repository, "cat-file", "-s", c.base_commit))
        if commit_size > 1024 * 1024:
            deny("patch_base_commit_limit")
        raw = self.source_git.run(c.repository, "cat-file", "commit", c.base_commit)
        actual = hashlib.sha1(b"commit " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if actual != c.base_commit:
            deny("patch_base_commit_corrupt")
        line = raw.split(b"\n", 1)[0]
        if len(line) != 45 or not line.startswith(b"tree "):
            deny("patch_base_tree_invalid")
        expected_tree = line[5:].decode("ascii")
        seen = {}
        all_names = set(base) | set(inputs.contents)
        for path in sorted(all_names):
            relative_path(path)
            unambiguous(path, seen)
            parts = path.split("/")
            if any("/".join(parts[:i]) in all_names for i in range(1, len(parts))):
                deny("patch_file_directory_collision")
        if any(not within(p, c.allowed) for p in inputs.contents):
            deny("patch_path_not_authorized", 403)
        self.invoke(
            None, "init", "--bare", "--template=" + str(self.config.hooks), str(scratch / "git")
        )
        self._index(scratch, base)
        base_tree = self.invoke(scratch, "write-tree", "--missing-ok").decode().strip()
        if base_tree != expected_tree:
            deny("patch_base_tree_mismatch")
        total = 0
        for path, content in inputs.contents.items():
            target = scratch / "work" / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if path in base and base[path][1] != blob_id(content):
                size = int(self.source_git.run(c.repository, "cat-file", "-s", base[path][1]))
                if size > 8 * 1024 * 1024 or total + size > 64 * 1024 * 1024:
                    deny("patch_base_blob_limit")
                original = self.source_git.run(c.repository, "cat-file", "blob", base[path][1])
                total += len(original)
                if len(original) != size:
                    deny("patch_base_blob_changed")
                if blob_id(original) != base[path][1]:
                    deny("patch_base_blob_corrupt")
                if (
                    self.invoke(scratch, "hash-object", "-w", "--stdin", data=original)
                    .decode()
                    .strip()
                    != base[path][1]
                ):
                    deny("patch_base_blob_corrupt")
        self.snapshot(scratch / "work", inputs.contents)
        expected = dict(base)
        for path, content in inputs.contents.items():
            sha = (
                self.invoke(scratch, "hash-object", "-w", "--stdin", data=content).decode().strip()
            )
            if sha != blob_id(content):
                deny("patch_output_blob_mismatch")
            expected[path] = (base[path][0] if path in base else "100644", sha)
        self._index(scratch, expected)
        changed = sorted(p for p in expected if expected[p] != base.get(p))
        tokens = self.invoke(
            scratch, "diff", "--cached", "--name-status", "-z", "--no-renames", base_tree, "--"
        ).split(b"\0")
        tokens = [x for x in tokens if x]
        actual = []
        if len(tokens) % 2:
            deny("patch_changed_paths_invalid")
        for status, name in zip(tokens[::2], tokens[1::2], strict=True):
            path = relative_path(name.decode("utf-8"))
            if status not in {b"A", b"M"} or not within(path, c.allowed):
                deny("patch_changed_path_not_authorized", 403)
            if status != (b"M" if path in base else b"A"):
                deny("patch_unsupported_change")
            actual.append(path)
        if sorted(actual) != changed or self._entries(scratch) != expected:
            deny("patch_index_mismatch")
        patch = self.invoke(
            scratch,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-color",
            "--no-relative",
            "--no-indent-heuristic",
            "--diff-algorithm=myers",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--unified=3",
            base_tree,
            "--",
        )
        if bool(patch) != bool(changed):
            deny("patch_diff_mismatch")
        self.snapshot(scratch / "work", inputs.contents)
        verify = scratch / "verify.index"
        self.invoke(scratch, "read-tree", base_tree, index=verify)
        if self._entries(scratch, verify) != base:
            deny("patch_clean_base_mismatch")
        if patch:
            self.invoke(
                scratch,
                "apply",
                "--cached",
                "--check",
                "--whitespace=nowarn",
                "-",
                data=patch,
                index=verify,
            )
            self.invoke(
                scratch, "apply", "--cached", "--whitespace=nowarn", "-", data=patch, index=verify
            )
        if self._entries(scratch, verify) != expected:
            deny("patch_applied_tree_mismatch")
        for path, content in inputs.contents.items():
            applied = self.invoke(scratch, "cat-file", "blob", expected[path][1])
            if (
                applied != content
                or hashlib.sha256(applied).hexdigest() != hashlib.sha256(content).hexdigest()
            ):
                deny("patch_applied_content_mismatch")
        result_tree = (
            self.invoke(scratch, "write-tree", "--missing-ok", index=verify).decode().strip()
        )
        self.snapshot(scratch / "work", inputs.contents)
        proof = BuildProof(
            base_commit=c.base_commit,
            base_tree=base_tree,
            result_tree=result_tree,
            git_version=self.version,
            import_manifest_sha256=c.manifest_sha256,
            changed_paths=changed,
            source_files=inputs.manifest.files,
            applicability="applicable" if patch else "empty_no_changes",
        )
        return patch, proof

    def construct(self, inputs):
        scratch = Path(tempfile.mkdtemp(prefix="build-", dir=self.root))
        (scratch / "work").mkdir(mode=0o700)
        try:
            return self._construct(scratch, inputs)
        finally:
            # Only our freshly allocated absolute directory; never follow a replacement root.
            safe_directory(self.root)
            plain(scratch, directory=True)
            if scratch.parent != self.root or not scratch.resolve().is_relative_to(
                self.root.resolve()
            ):
                deny("patch_unsafe_cleanup_root")

            def clear_readonly(function, path, error):
                entry = Path(path)
                if not isinstance(error, PermissionError) or not entry.resolve().is_relative_to(
                    scratch.resolve()
                ):
                    raise error
                plain(entry)
                entry.chmod(entry.stat().st_mode | 0o200)
                function(path)

            shutil.rmtree(scratch, onexc=clear_readonly)
