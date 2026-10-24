"""Exclusive private capsule authenticated by a per-import DB key; never an Agent input."""

import hashlib
import hmac
import json
import os

from hogwarts.artifacts.filesystem import LocalObjects
from hogwarts.outputs.contracts import MAX_ARCHIVE, deny


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class CapsuleVault:
    def __init__(self, root, excluded):
        self.objects = LocalObjects(root, excluded)

    def exists(self, row):
        return os.path.lexists(self.objects.path(row.id))

    def seal(self, row, archive, proof):
        header = {
            "schema_version": 1,
            "import_id": row.id,
            "run_id": row.run_id,
            "workspace_id": row.workspace_id,
            "sandbox_id": row.sandbox_id,
            "request_hash": row.request_hash,
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "proof": proof,
        }
        raw_header = canonical(header)
        body = len(raw_header).to_bytes(4, "big") + raw_header + archive
        tag = hmac.new(bytes.fromhex(row.seal_key), body, hashlib.sha256).digest()
        data = tag + body
        if len(data) > MAX_ARCHIVE:
            deny("snapshot_capsule_limit")
        self.objects.publish(row.id, data, hashlib.sha256(data).hexdigest())
        return self.open(row)

    def open(self, row):
        path = self.objects.path(row.id)
        self.objects._regular(path.lstat())
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            info = self.objects._check_fd(path, stream.fileno())
            if info.st_size > MAX_ARCHIVE or info.st_size < 36:
                deny("snapshot_capsule_invalid")
            data = stream.read(MAX_ARCHIVE + 1)
        # Re-read with all M2.3.1 integrity and TOCTOU checks before trusting these bytes.
        data = self.objects.read(row.id, hashlib.sha256(data).hexdigest(), len(data))
        tag, body = data[:32], data[32:]
        expected = hmac.new(bytes.fromhex(row.seal_key), body, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            deny("snapshot_seal_invalid")
        length = int.from_bytes(body[:4], "big")
        if length > 16384 or length + 4 > len(body):
            deny("snapshot_capsule_invalid")
        header = json.loads(body[4 : 4 + length])
        archive = body[4 + length :]
        if (
            header.get("schema_version") != 1
            or header.get("import_id") != row.id
            or header.get("run_id") != row.run_id
            or header.get("workspace_id") != row.workspace_id
            or header.get("sandbox_id") != row.sandbox_id
            or header.get("request_hash") != row.request_hash
            or header.get("archive_sha256") != hashlib.sha256(archive).hexdigest()
        ):
            deny("snapshot_binding_invalid")
        return archive, header["proof"]
