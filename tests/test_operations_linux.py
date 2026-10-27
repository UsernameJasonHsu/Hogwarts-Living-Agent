"""Actual Docker verification and reviewed GC of independent disposable fixtures."""

import hashlib
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from hogwarts.models import IdempotencyRecord, PrototypeRun, now
from hogwarts.operations.contracts import Capacity, QuotaProfile, VersionedRetentionPolicy
from hogwarts.operations.controller import OperationalPrototypeController
from hogwarts.operations.health import OperationalHealth
from hogwarts.operations.quota import AggregateResourceQuotaManager
from hogwarts.operations.retention import SafeGarbageCollectionPlanner
from hogwarts.operations.storage import StorageUsageAccounting
from hogwarts.reconciliation.docker import DockerResourceBackend
from recovery_support import real_orchestrator
from test_verification import HUMAN, review
from test_workspaces import workspace as workspace

pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or not all(
        os.getenv(x)
        for x in (
            "HOGWARTS_SANDBOX_IMAGE",
            "HOGWARTS_OUTPUT_READER_IMAGE",
            "HOGWARTS_REVIEW_QA_IMAGE",
        )
    ),
    reason="Real Linux Docker operations require Linux and pinned runtime/reader/QA images",
)


@pytest.mark.parametrize("mode", ["approved", "held", "expired-approval"])
def test_real_linux_admission_and_retention(workspace, mode):
    w = workspace
    o = real_orchestrator(w["manager"], w["root"].parent / "operations-real", "m244-" + uuid4().hex)
    backend = DockerResourceBackend(o.controller, o.qa)
    roots = (
        w["root"],
        o.store.objects.root,
        o.importer.vault.objects.root,
        o.builder.objects.root,
        o.qa.vault.objects.root,
        o.vault.objects.root,
    )
    storage = StorageUsageAccounting({HUMAN.owner_id: roots}, min_free_bytes=0)
    q = AggregateResourceQuotaManager(
        w["manager"],
        {
            HUMAN.owner_id: Capacity(
                slots=2, cpu_millis=4000, memory_bytes=2**31, pids=512, storage_bytes=2**30
            )
        },
        (
            QuotaProfile(
                name="prototype",
                runtime_profile="capture",
                qa_profile="acceptance",
                demand=Capacity(
                    slots=1, cpu_millis=1000, memory_bytes=2**28, pids=64, storage_bytes=2**27
                ),
            ),
        ),
        storage,
        backend=backend,
    )
    q.initialize(HUMAN, w["run"])
    g = SafeGarbageCollectionPlanner(
        w["manager"],
        VersionedRetentionPolicy(name="ephemeral", ephemeral_ttl_seconds=0),
        backend=backend,
        store=o.store,
    )

    def hashes():
        return {
            f"root-{i}/{p.relative_to(root)}": hashlib.sha256(p.read_bytes()).hexdigest()
            for i, root in enumerate(roots[1:])
            for p in root.rglob("*")
            if p.is_file()
        }

    try:
        info = OperationalPrototypeController(q, o).run(
            w["actor"], w["run"], "real", w["token"], "prototype"
        )
        assert info.status == "READY_FOR_HUMAN_REVIEW"
        review((w, o), info, decision="REJECTED")
        before = hashes()
        with w["factory"].begin() as s:
            s.get(PrototypeRun, w["run"]).lease_expires_at = now() - timedelta(days=2)
        if mode == "held":
            g.references.hold(HUMAN, w["run"], 0, reason="review")
        p = g.plan(HUMAN, w["run"], "real-gc")
        result = p
        if mode != "held":
            approved = g.approve(HUMAN, w["run"], p.plan_id, p.version)
            if mode == "expired-approval":
                with w["factory"].begin() as s:
                    row = s.get(IdempotencyRecord, p.plan_id)
                    row.response = {
                        **row.response,
                        "approval_expires_at": (now() - timedelta(seconds=1)).isoformat(),
                    }
            result = g.execute(HUMAN, w["run"], p.plan_id, approved.version)
            assert result.state == ("COMPLETE" if mode == "approved" else "BLOCKED"), result
        else:
            assert not p.eligible
        assert before == hashes()
        h = OperationalHealth(q, store=o.store).snapshot(HUMAN, w["run"])
        assert h["status"] != "unknown", h
        directory = os.getenv("HOGWARTS_VERIFICATION_EVIDENCE_DIR")
        if directory:
            target = Path(directory) / ("operations-" + mode + "-" + w["run"])
            target.mkdir(parents=True)
            for name, data in (
                ("dry-run", p.model_dump(mode="json")),
                ("gc-result", result.model_dump(mode="json")),
                ("health", h),
                ("preserved-hashes", before),
            ):
                (target / (name + ".json")).write_text(json.dumps(data, indent=2), encoding="utf8")
    finally:
        from hogwarts.leases.manager import DurableLeaseManager
        from hogwarts.reconciliation.planner import ResourceReconciler

        with w["factory"]() as s:
            generation = s.get(PrototypeRun, w["run"]).lease_generation
        DurableLeaseManager(w["factory"]).revoke(
            HUMAN, w["run"], generation, "operations-test-finish"
        )
        cleanup = ResourceReconciler(w["manager"], backend=backend)
        planned = cleanup.plan(HUMAN, w["run"], "test-finally")
        cleaned = cleanup.execute(HUMAN, w["run"], planned.plan_id, planned.version)
        assert cleaned.state == "COMPLETE", cleaned
