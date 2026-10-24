"""Bounded USTAR-only parser. Never extract, decompress or process extension records."""

import hashlib

from hogwarts.outputs.contracts import (
    MAX_ARCHIVE,
    MAX_ENTRIES,
    MAX_FILE,
    MAX_FILES,
    MAX_TOTAL,
    OutputFile,
    deny,
)
from hogwarts.service import DomainError
from hogwarts.workspaces.git import relative_path, unambiguous, within


def field(raw):
    value, _, padding = raw.partition(b"\0")
    if padding.strip(b"\0"):
        deny("archive_noncanonical_field")
    return value.decode("utf-8", errors="strict")


def octal(raw):
    value = raw.strip(b"\0 ")
    if not value or any(c not in b"01234567" for c in value):
        deny("archive_invalid_number")
    return int(value, 8)


def validate_archive(data: bytes, prefix: str, allowed: tuple[str, ...]) -> tuple[OutputFile, ...]:
    try:
        return _validate(data, prefix, allowed)
    except (UnicodeError, ValueError):
        deny("archive_invalid_encoding")
    except DomainError as error:
        from hogwarts.outputs.contracts import OutputError

        if isinstance(error, OutputError):
            raise
        deny("archive_unsafe_path", 403)


def _validate(data, prefix, allowed):
    relative_path(prefix)
    if type(data) is not bytes or len(data) > MAX_ARCHIVE or len(data) < 1024 or len(data) % 512:
        deny("archive_size_or_format")
    paths, case, ancestors, files = {}, {}, set(), []
    offset = total = count = file_count = 0
    while offset + 512 <= len(data):
        header = data[offset : offset + 512]
        if not any(header):
            if offset + 1024 > len(data) or any(data[offset:]):
                deny("archive_unsafe_trailer")
            return tuple(sorted(files, key=lambda f: f.target_path))
        count += 1
        if count > MAX_ENTRIES:
            deny("archive_too_many_entries")
        if header[257:263] != b"ustar\0" or header[263:265] != b"00":
            deny("archive_unsupported_format")
        if octal(header[148:156]) != sum(header[:148]) + 8 * 32 + sum(header[156:]):
            deny("archive_checksum_failed")
        kind = header[156:157]
        if kind not in {b"0", b"\0", b"5"} or any(header[157:257]):
            deny("archive_links_or_special_type")
        size, mode = octal(header[124:136]), octal(header[100:108])
        octal(header[108:116])
        octal(header[116:124])
        octal(header[136:148])
        if mode & ~0o777 or size > MAX_FILE or (kind == b"5" and size):
            deny("archive_file_limit")
        name, parent = field(header[:100]), field(header[345:500])
        path = (parent + "/" if parent else "") + name
        if kind == b"5" and path.endswith("/"):
            path = path[:-1]
        relative_path(path)
        unambiguous(path, case)
        parts = path.split("/")
        if path in paths or any(
            paths.get("/".join(parts[:i])) == "file" for i in range(1, len(parts))
        ):
            deny("archive_duplicate_or_collision")
        if kind != b"5" and path in ancestors:
            deny("archive_duplicate_or_collision")
        paths[path] = "dir" if kind == b"5" else "file"
        ancestors.update("/".join(parts[:i]) for i in range(1, len(parts)))
        target = relative_path(prefix + "/" + path)
        if path == ".executed" and kind == b"5":
            deny("archive_invalid_control_marker")
        if kind == b"5":
            if not within(target, allowed) and not any(g.startswith(target + "/") for g in allowed):
                deny("output_path_not_granted", 403)
        else:
            file_count += 1
            total += size
            if file_count > MAX_FILES or total > MAX_TOTAL:
                deny("archive_payload_limit")
        start, end = offset + 512, offset + 512 + size
        next_offset = start + ((size + 511) // 512) * 512
        if next_offset > len(data) or any(data[end:next_offset]):
            deny("archive_truncated_or_padding")
        if kind != b"5":
            if path == ".executed":
                if size:
                    deny("archive_invalid_control_marker")
            else:
                if not within(target, allowed):
                    deny("output_path_not_granted", 403)
                content = data[start:end]
                files.append(OutputFile(path, target, content, hashlib.sha256(content).hexdigest()))
        offset = next_offset
    deny("archive_missing_end")
