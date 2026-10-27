"""Recovery may not turn cleanup credentials into historical Artifact read rights."""

from datetime import timedelta

import pytest
from hogwarts.leases.manager import DurableLeaseManager
from hogwarts.models import PrototypeRun, now
from hogwarts.service import DomainError
from sqlalchemy import select
from test_recovery import coordinator
from test_recovery_states import partial
from test_verification import HUMAN, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


@pytest.mark.parametrize("state", ["expired", "revoked", "completed"])
def test_worker_cannot_read_historical_artifact_bytes(verification, monkeypatch, state):
    v = verification
    w = v[0]
    if state == "completed":
        verify(v)
    else:
        partial(v, monkeypatch, "vb_")
        if state == "expired":
            with w["factory"].begin() as s:
                s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(seconds=1)
        else:
            DurableLeaseManager(w["factory"]).revoke(HUMAN, w["run"], 1, "read-revocation")
    reads = []
    original = v[1].store.objects.read

    def tracked(*args, **kwargs):
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1].store.objects, "read", tracked)
    with pytest.raises(DomainError):
        coordinator(v).recover(w["actor"], w["run"], "worker-new-inspection", w["token"])
    assert not reads
    owner = coordinator(v).recover(HUMAN, w["run"], "owner-historical-inspection")
    assert owner.state == "PLANNED" and reads


def test_worker_acknowledges_publisher_receipt_without_postterminal_reads(
    verification, monkeypatch
):
    v = verification
    w = v[0]
    partial(v, monkeypatch, "vb_")
    c = coordinator(v)
    p = c.recover(w["actor"], w["run"], "receipt", w["token"])
    original = v[1].store.objects.read
    reads = []

    def live_only(*args, **kwargs):
        with w["factory"]() as s:
            status = s.scalar(select(PrototypeRun.status).where(PrototypeRun.id == w["run"]))
        assert status == "CLAIMED", "worker must not read bytes after terminal publication"
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(v[1].store.objects, "read", live_only)
    done = c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"])
    assert done.state == "COMPLETE", done
    assert done.result.decision.reason == "recovery_trusted_publisher_committed"
    assert reads
    assert c.execute(w["actor"], w["run"], p.recovery_id, p.version, w["token"]) == done


def test_inspection_does_not_read_unpublished_artifact_bytes(verification, monkeypatch):
    from hogwarts.models import ArtifactMetadata

    v = verification
    w = v[0]
    partial(v, monkeypatch, "qa_")
    with w["factory"].begin() as s:
        row = next(x for x in s.scalars(select(ArtifactMetadata)) if x.evidence["kind"] == "log")
        identifier = row.id
        row.state = "PENDING"
    original = v[1].store.objects.read

    def published_only(artifact_id, *args, **kwargs):
        assert artifact_id != identifier, "PENDING object requires its exact authorized put retry"
        return original(artifact_id, *args, **kwargs)

    monkeypatch.setattr(v[1].store.objects, "read", published_only)
    plan = coordinator(v).recover(w["actor"], w["run"], "pending-read", w["token"])
    assert plan.observation.decision.classification == "SAFE_RESUME", plan
    assert identifier not in plan.observation.artifact_hashes
