import os
from types import SimpleNamespace

import pytest
from hogwarts.operations.health import OperationalHealth
from hogwarts.operations.storage import StorageUsageAccounting
from hogwarts.service import DomainError
from test_operations import quota
from test_verification import HUMAN
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def test_storage_counts_actual_bytes_not_declared_spend(verification):
    v = verification
    root = v[1].store.objects.root
    (root / "fixture.txt").write_bytes(b"12345")
    storage = StorageUsageAccounting({HUMAN.owner_id: (root,)}, min_free_bytes=0)
    with v[0]["factory"]() as s:
        usage = storage.measure(s, HUMAN.owner_id)
    assert usage.logical_bytes == 5 and usage.declared_artifact_bytes == 0


@pytest.mark.parametrize("failure", ["disk", "io", "changing", "limit"])
def test_storage_fail_closed(verification, monkeypatch, failure):
    v = verification
    root = v[1].store.objects.root
    storage = StorageUsageAccounting({HUMAN.owner_id: (root,)}, min_free_bytes=0)
    if failure == "disk":
        storage.disk_usage = lambda _: SimpleNamespace(free=0)
    elif failure == "io":

        def enospc(_):
            raise OSError(28, "injected ENOSPC")

        storage.disk_usage = enospc
    elif failure == "changing":
        original = storage.tree
        calls = [0]

        def change(p):
            calls[0] += 1
            if calls[0] == 2:
                (root / "new.txt").write_bytes(b"changed")
            return original(p)

        monkeypatch.setattr(storage, "tree", change)
    else:
        (root / "too-large.txt").write_bytes(b"12345")
        storage.max_bytes = 4
    q = quota(v, storage=storage)
    q.initialize(HUMAN, v[0]["run"])
    with pytest.raises(DomainError):
        q.reserve(v[0]["actor"], v[0]["run"], "no", v[0]["token"], "prototype")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_storage_rejects_links_and_special_files(verification, kind):
    v = verification
    root = v[1].store.objects.root
    target = root / "file.txt"
    target.write_bytes(b"protected")
    other = root / "unsafe"
    try:
        if kind == "symlink":
            other.symlink_to(target)
        elif kind == "hardlink":
            os.link(target, other)
        elif hasattr(os, "mkfifo"):
            os.mkfifo(other)
        else:
            pytest.skip("FIFO unavailable on this development host; Linux verifies")
    except OSError:
        pytest.skip("Native link creation unavailable; Linux verifies")
    try:
        storage = StorageUsageAccounting({HUMAN.owner_id: (root,)})
        with v[0]["factory"]() as s, pytest.raises(DomainError):
            storage.measure(s, HUMAN.owner_id)
    finally:
        other.unlink()


def test_root_scope_and_health_manual_incident(verification):
    v = verification
    root = v[1].store.objects.root
    with pytest.raises(DomainError):
        StorageUsageAccounting({"one": (root,), "two": (root,)})
    q = quota(v)
    q.initialize(HUMAN, v[0]["run"])
    h = OperationalHealth(q, manual_incidents=("one", "two", "three", "four"))
    result = h.snapshot(HUMAN, v[0]["run"])
    assert result["status"] == "blocked" and result["manual_incident_count"] == 4
    assert result["budget_unknown_actual_rows"] == 1 and not result["m3_ready"]
    assert str(root) not in str(result) and v[0]["token"] not in str(result)
