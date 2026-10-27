from datetime import timedelta

import pytest
from hogwarts.models import IdempotencyRecord, Proposal, PrototypeRun, now
from hogwarts.operations.contracts import VersionedRetentionPolicy
from hogwarts.operations.references import ReferenceProtector
from hogwarts.operations.retention import SafeGarbageCollectionPlanner
from hogwarts.reconciliation.contracts import Resource
from hogwarts.service import DomainError, digest
from test_verification import HUMAN, review, verify
from test_verification import verification as verification
from test_workspaces import workspace as workspace


class EmptyBackend:
    def inventory(self, s, run, ws):
        return []


def gc(v, *, ttl=0, backend=None):
    return SafeGarbageCollectionPlanner(
        v[0]["manager"],
        VersionedRetentionPolicy(name="ephemeral", ephemeral_ttl_seconds=ttl),
        backend=backend or EmptyBackend(),
        store=v[1].store,
    )


def expire(v):
    with v[0]["factory"].begin() as s:
        s.get(PrototypeRun, v[0]["run"]).lease_expires_at = now() - timedelta(days=2)


def prepared(v):
    w = v[0]
    info = w["manager"].create(w["actor"], w["run"], w["token"])
    expire(v)
    return w["root"] / info.id


def test_retention_requires_separate_human_approval(verification):
    v = verification
    path = prepared(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "dry")
    assert p.eligible and p.state == "DRY_RUN" and path.exists()
    assert p == g.plan(HUMAN, v[0]["run"], "dry")
    with pytest.raises(DomainError, match="retention_human_approval_required"):
        g.execute(HUMAN, v[0]["run"], p.plan_id, p.version)
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    assert a == g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    result = g.execute(HUMAN, v[0]["run"], p.plan_id, a.version)
    assert result.state == "COMPLETE" and not path.exists()
    assert result == g.execute(HUMAN, v[0]["run"], p.plan_id, a.version)


@pytest.mark.parametrize("hold", ["lease", "ttl", "review", "explicit", "unknown", "quarantine"])
def test_retention_protections(verification, hold):
    v = verification
    w = v[0]
    w["manager"].create(w["actor"], w["run"], w["token"])
    if hold != "lease":
        expire(v)
    g = gc(v, ttl=31536000 if hold == "ttl" else 0)
    if hold == "review":
        with w["factory"].begin() as s:
            s.get(Proposal, w["proposal"]).status = "REVIEWING"
    elif hold == "explicit":
        g.references.hold(HUMAN, w["run"], 0, reason="incident")
    elif hold == "unknown":
        with w["factory"].begin() as s:
            s.get(PrototypeRun, w["run"]).status = "UNKNOWN"
    elif hold == "quarantine":

        class Unknown(EmptyBackend):
            def inventory(self, s, run, ws):
                return [
                    Resource(
                        key="unknown:fixture",
                        kind="unknown",
                        identifier="disposable-fixture",
                        fingerprint=digest("fixture"),
                        disposition="QUARANTINE",
                        reason="unknown_owner",
                    )
                ]

        g = gc(v, backend=Unknown())
    p = g.plan(HUMAN, w["run"], "blocked")
    assert not p.eligible and p.reasons
    with pytest.raises(DomainError):
        g.approve(HUMAN, w["run"], p.plan_id, p.version)


@pytest.mark.parametrize("change", ["hold", "generation", "policy", "path", "approval"])
def test_gc_rechecks_before_effects(verification, change):
    v = verification
    path = prepared(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "plan")
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    if change == "hold":
        g.references.hold(HUMAN, v[0]["run"], 0)
    elif change == "generation":
        with v[0]["factory"].begin() as s:
            r = s.get(PrototypeRun, v[0]["run"])
            r.lease_generation += 1
            r.lease_version += 1
            r.lease_revoked_at = now()
    elif change == "policy":
        g.policy = g.policy.model_copy(update={"version": 2})
    elif change == "path":
        (path / "src/main.txt").write_bytes(b"changed after approval")
    else:
        with v[0]["factory"].begin() as s:
            row = s.get(IdempotencyRecord, p.plan_id)
            row.response = {
                **row.response,
                "approval_expires_at": (now() - timedelta(seconds=1)).isoformat(),
            }
    done = g.execute(HUMAN, v[0]["run"], p.plan_id, a.version)
    assert done.state == "BLOCKED" and path.exists(), done


def test_permanent_evidence_kept_after_expiry(verification):
    v = verification
    info = verify(v)
    review(v, info, decision="REJECTED")
    objects = v[1].store.objects
    before = {p.name: p.read_bytes() for p in objects.objects.iterdir()}
    expire(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "evidence")
    assert p.eligible, p
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    assert g.execute(HUMAN, v[0]["run"], p.plan_id, a.version).state == "COMPLETE"
    assert before == {p.name: p.read_bytes() for p in objects.objects.iterdir()}
    assert (
        v[1].store.read(HUMAN, v[0]["run"], info.bundle_artifact_id).evidence.assessment.status
        == "unverified"
    )


def test_hold_owner_only_and_cas(verification):
    v = verification
    h = ReferenceProtector(v[0]["manager"])
    with pytest.raises(DomainError):
        h.hold(v[0]["actor"], v[0]["run"], 0)
    held = h.hold(HUMAN, v[0]["run"], 0)
    with pytest.raises(DomainError):
        h.hold(HUMAN, v[0]["run"], 0, active=False)
    assert not h.hold(HUMAN, v[0]["run"], held.version, active=False).active


def test_late_hold_inside_reconciler_guard_blocks_delete(verification, monkeypatch):
    from hogwarts.reconciliation.executor import CleanupExecutor

    v = verification
    path = prepared(v)
    g = gc(v)
    p = g.plan(HUMAN, v[0]["run"], "late")
    a = g.approve(HUMAN, v[0]["run"], p.plan_id, p.version)
    original = CleanupExecutor.execute

    def late(executor, *args):
        g.references.hold(HUMAN, v[0]["run"], 0)
        return original(executor, *args)

    monkeypatch.setattr(CleanupExecutor, "execute", late)
    done = g.execute(HUMAN, v[0]["run"], p.plan_id, a.version)
    assert done.state == "UNKNOWN" and path.exists()
    again = g.execute(HUMAN, v[0]["run"], p.plan_id, done.version)
    assert again == done and path.exists()
