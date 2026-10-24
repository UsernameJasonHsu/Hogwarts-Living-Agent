"""Local-only Git primitives. No shell, network operations or project command execution."""

import configparser
import hashlib
import os
import re
import shutil
import stat
import subprocess
import unicodedata
from pathlib import Path
from uuid import uuid4

from hogwarts.workspaces.contracts import WorkspaceError

SHA = re.compile(r"[0-9a-f]{40}\Z")
DEVICE = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?\Z", re.I)


def reject(code, status=409):
    raise WorkspaceError(status, code)


def relative_path(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or unicodedata.normalize("NFC", value) != value
    ):
        reject("unsafe_path", 403)
    for part in value.split("/"):
        if (
            not part
            or part in {".", ".."}
            or part.casefold() in {".git", "git~1", ".gitattributes", ".gitmodules", ".lfsconfig"}
            or part.endswith((" ", "."))
            or DEVICE.fullmatch(part)
            or any(ord(c) < 32 or c in '\\:*?[]%<>"|' for c in part)
        ):
            reject("unsafe_path", 403)
    return value


def within(path, grants):
    return any(path == grant or path.startswith(grant + "/") for grant in grants)


def unambiguous(path, seen):
    parts = path.split("/")
    for length in range(1, len(parts) + 1):
        prefix = "/".join(parts[:length])
        previous = seen.setdefault(prefix.casefold(), prefix)
        if previous != prefix:
            reject("ambiguous_case_path", 403)


def plain(path, *, directory=False):
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        reject("unsafe_filesystem_entry", 403)
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            reject("unsafe_filesystem_entry", 403)
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        reject("unsafe_filesystem_entry", 403)
    return info


def safe_directory(path):
    path = Path(os.path.abspath(path))
    for item in reversed([path, *path.parents]):
        if os.path.lexists(item):
            plain(item, directory=True)
    return path


def walk_files(root, *, git_file=False):
    plain(root, directory=True)
    result = {}
    stack = [root]
    while stack:
        folder = stack.pop()
        for entry in folder.iterdir():
            rel = entry.relative_to(root).as_posix()
            if git_file and rel == ".git":
                plain(entry)
                continue
            info = entry.lstat()
            if stat.S_ISDIR(info.st_mode):
                plain(entry, directory=True)
                stack.append(entry)
            else:
                plain(entry)
                result[rel] = info
            if len(result) > 10000:
                reject("workspace_too_large")
    return result


class LocalGit:
    def __init__(self, root: Path, executable: Path | None = None):
        self.root = safe_directory(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.private = self.root / ".manager"
        self.private.mkdir(exist_ok=True)
        safe_directory(self.private)
        self.hooks = self.private / "hooks"
        self.hooks.mkdir(exist_ok=True)
        plain(self.hooks, directory=True)
        self.empty = self.private / "empty.config"
        if not os.path.lexists(self.empty):
            try:
                with self.empty.open("xb"):
                    pass
            except FileExistsError:
                pass  # Another manager instance initialized the same trusted root.
        plain(self.empty)
        if self.empty.read_bytes() or any(self.hooks.iterdir()):
            reject("manager_metadata_tampered")
        self.executable = Path(executable or shutil.which("git") or "").absolute()
        if not self.executable.is_file():
            reject("git_unavailable")

    def environment(self, index=None):
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}
        env.update(
            {
                "PATH": str(self.executable.parent) + os.pathsep + os.defpath,
                "HOME": str(self.private),
                "USERPROFILE": str(self.private),
                "XDG_CONFIG_HOME": str(self.private),
                "TMP": str(self.private),
                "TEMP": str(self.private),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(self.empty),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_LFS_SKIP_SMUDGE": "1",
                "LC_ALL": "C",
                "GIT_ATTR_NOSYSTEM": "1",
            }
        )
        if index is not None:
            env["GIT_INDEX_FILE"] = str(index)
        return env

    def run(self, repo, *args, worktree=None, index=None):
        command = [
            str(self.executable),
            "--no-pager",
            "-c",
            f"core.hooksPath={self.hooks}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "core.protectNTFS=true",
            "-c",
            f"core.attributesFile={self.empty}",
            "-c",
            "protocol.allow=never",
            "-c",
            "submodule.recurse=false",
            "-C",
            str(worktree if worktree is not None else repo),
        ]
        if worktree is not None:
            command += [
                "--git-dir",
                str(repo / ".git/worktrees" / worktree.name),
                "--work-tree",
                str(worktree),
            ]
        try:
            result = subprocess.run(
                [*command, *args],
                env=self.environment(index),
                shell=False,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            reject("git_timeout")
        except OSError:
            reject("git_execution_failed")
        if result.returncode:
            reject("git_command_failed")
        if len(result.stdout) > 16 * 1024 * 1024:
            reject("git_output_too_large")
        return result.stdout

    def repository(self, path):
        repo = safe_directory(path)
        plain(repo / ".git", directory=True)
        walk_files(repo / ".git")
        config = configparser.RawConfigParser(strict=False)
        try:
            config.read_string((repo / ".git/config").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, configparser.Error):
            reject("unsafe_repository_config", 403)
        if config.defaults():
            reject("unsafe_repository_config", 403)
        allowed_core = {
            "repositoryformatversion",
            "filemode",
            "bare",
            "logallrefupdates",
            "symlinks",
            "ignorecase",
            "autocrlf",
            "safecrlf",
            "precomposeunicode",
            "quotepath",
            "longpaths",
        }
        for section in config.sections():
            if section == "core":
                if set(config[section]) - allowed_core:
                    reject("unsafe_repository_config", 403)
            elif section == "user":
                if set(config[section]) - {"name", "email"}:
                    reject("unsafe_repository_config", 403)
            elif re.fullmatch(r'remote "[^"\r\n]+"', section):
                if set(config[section]) - {"url", "fetch", "pushurl"}:
                    reject("unsafe_repository_config", 403)
            elif re.fullmatch(r'branch "[^"\r\n]+"', section):
                if set(config[section]) - {"remote", "merge", "rebase", "description"}:
                    reject("unsafe_repository_config", 403)
            else:
                reject("unsafe_repository_config", 403)
        if config.get("core", "bare", fallback="false").lower() != "false":
            reject("unsupported_repository", 403)
        for name in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts"):
            if (repo / ".git" / name).exists():
                reject("unsafe_repository_config", 403)
        top = Path(os.fsdecode(self.run(repo, "rev-parse", "--show-toplevel").strip()))
        if (
            top.resolve() != repo.resolve()
            or repo == self.root
            or repo in self.root.parents
            or self.root in repo.parents
        ):
            reject("unsafe_workspace_root", 403)
        return repo

    def tree(self, repo, commit):
        if not SHA.fullmatch(commit):
            reject("invalid_commit", 422)
        if self.run(repo, "cat-file", "-t", commit).strip() != b"commit":
            reject("not_a_commit", 422)
        result = {}
        folded = {}
        for record in self.run(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
            if not record:
                continue
            meta, raw_path = record.split(b"\t", 1)
            mode, kind, sha = meta.split()
            try:
                path = relative_path(raw_path.decode("utf-8"))
            except UnicodeError:
                reject("unsafe_path", 403)
            if mode not in {b"100644", b"100755"} or kind != b"blob":
                reject("unsafe_git_tree", 403)
            unambiguous(path, folded)
            result[path] = (mode.decode(), sha.decode())
        return result

    def provenance(self, repo, path, commit=None):
        safe_directory(path)
        plain(path / ".git")
        gitdir = repo / ".git/worktrees" / path.name
        plain(gitdir, directory=True)
        plain(gitdir / "gitdir")
        plain(gitdir / "commondir")
        pointer = (path / ".git").read_text(encoding="utf-8").strip()
        backlink = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
        if pointer != "gitdir: " + gitdir.as_posix() and pointer != "gitdir: " + str(gitdir):
            reject("workspace_provenance_mismatch", 403)
        if Path(backlink).resolve() != (path / ".git").resolve():
            reject("workspace_provenance_mismatch", 403)
        if (gitdir / "commondir").read_text().strip() != "../..":
            reject("workspace_provenance_mismatch", 403)
        if commit is not None:
            plain(gitdir / "HEAD")
            if (gitdir / "HEAD").read_text().strip() != commit:
                reject("workspace_head_mismatch", 403)

    def snapshot(self, path, baseline):
        result = {}
        folded = {}
        total = 0
        for relative, info in walk_files(path, git_file=True).items():
            relative_path(relative)
            unambiguous(relative, folded)
            total += info.st_size
            if info.st_size > 8 * 1024 * 1024 or total > 64 * 1024 * 1024:
                reject("workspace_too_large")
            data = (path / relative).read_bytes()
            sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            mode = (
                baseline.get(relative, ("100644",))[0]
                if os.name == "nt"
                else "100755"
                if info.st_mode & stat.S_IXUSR
                else "100644"
            )
            result[relative] = (mode, sha)
        return result

    def add(self, repo, path, commit):
        self.run(repo, "worktree", "add", "--detach", "--no-guess-remote", "--", str(path), commit)
        self.provenance(repo, path, commit)

    def remove(self, repo, path):
        metadata = repo / ".git/worktrees" / path.name
        if os.path.lexists(path):
            self.provenance(repo, path)
            walk_files(path, git_file=True)
        elif not metadata.exists():
            return
        else:
            plain(metadata, directory=True)
            plain(metadata / "gitdir")
            if Path((metadata / "gitdir").read_text().strip()).absolute() != path / ".git":
                reject("workspace_provenance_mismatch", 403)
        self.run(repo, "worktree", "remove", "--force", "--", str(path))

    def diff(self, repo, path, commit, allowed):
        baseline = self.tree(repo, commit)
        self.provenance(repo, path, commit)
        snapshot = self.snapshot(path, baseline)
        changed = tuple(
            sorted(
                name
                for name in baseline.keys() | snapshot.keys()
                if baseline.get(name) != snapshot.get(name)
            )
        )
        if any(not within(name, allowed) for name in changed):
            reject("changed_path_not_authorized", 403)
        index = self.private / (uuid4().hex + ".index")
        try:
            self.run(repo, "read-tree", commit, worktree=path, index=index)
            self.run(repo, "add", "--all", "--force", "--", ".", worktree=path, index=index)
            names = self.run(
                repo,
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--no-renames",
                commit,
                "--",
                worktree=path,
                index=index,
            )
            actual = tuple(sorted(p.decode("utf-8") for p in names.split(b"\0") if p))
            patch = self.run(
                repo,
                "diff",
                "--cached",
                "--binary",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                commit,
                "--",
                worktree=path,
                index=index,
            )
            if actual != changed or self.snapshot(path, baseline) != snapshot:
                reject("workspace_changed_during_export")
            return changed, patch
        finally:
            for file in (index, Path(str(index) + ".lock")):
                if file.exists():
                    plain(file)
                    file.unlink()
