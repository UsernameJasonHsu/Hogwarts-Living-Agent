"""M2.5 contracts before implementation. Workload adapters here are explicit fakes."""

import pytest
from conftest import headers
from fastapi.testclient import TestClient
from formal_identity import inprocess_service
from hogwarts.entrypoints.gateway import FormalExecutionGateway
from hogwarts.models import Proposal
from hogwarts.operations.contracts import Capacity, QuotaProfile
from hogwarts.operations.quota import AggregateResourceQuotaManager
from hogwarts.operations.storage import StorageUsageAccounting
from hogwarts.service import DomainError
from test_operations_retention import EmptyBackend
from test_verification import HUMAN
from test_verification import verification as verification
from test_workspaces import workspace as workspace


def gateway(v, *, slots=2):
    from dataclasses import replace
    from types import MappingProxyType

    w, o = v[:2]
    o.controller.profiles = MappingProxyType(
        {name: replace(p, lifetime_seconds=60) for name, p in o.controller.profiles.items()}
    )
    roots = (
        w["root"],
        o.store.objects.root,
        o.importer.vault.objects.root,
        o.builder.objects.root,
        o.qa.vault.objects.root,
        o.vault.objects.root,
    )
    q = AggregateResourceQuotaManager(
        w["manager"],
        {
            HUMAN.owner_id: Capacity(
                slots=slots, cpu_millis=4000, memory_bytes=2**30, pids=512, storage_bytes=2**29
            )
        },
        (
            QuotaProfile(
                name="prototype",
                runtime_profile="capture",
                qa_profile="acceptance",
                demand=Capacity(
                    slots=1, cpu_millis=1000, memory_bytes=2**28, pids=64, storage_bytes=2**20
                ),
            ),
        ),
        StorageUsageAccounting({HUMAN.owner_id: roots}, min_free_bytes=0),
        backend=EmptyBackend(),
    )
    g = FormalExecutionGateway(q, o, "prototype")
    g.initialize(HUMAN, w["run"])
    return g


def version(w):
    with w["factory"]() as s:
        return s.get(Proposal, w["proposal"]).version


def test_formal_http_executes_once_with_quota(verification):
    v = verification
    w = v[0]
    g = gateway(v)
    app = inprocess_service(g)
    body = {"expected_version": version(w), "lease_token": w["token"]}
    with TestClient(app) as client:
        a = client.post(
            f"/api/v1/runs/{w['run']}/execute", json=body, headers=headers("worker", "formal")
        )
        b = client.post(
            f"/api/v1/runs/{w['run']}/execute", json=body, headers=headers("worker", "formal")
        )
    assert a.status_code == b.status_code == 200, a.text
    assert a.json() == b.json() and a.json()["verified"] is False
    assert v[3].calls.count("execute") == 1


@pytest.mark.parametrize("effect", ["workspace", "sandbox", "qa", "verify", "quota", "objects"])
def test_bound_graph_has_no_raw_effect_bypass(verification, effect):
    v = verification
    w, o = v[:2]
    g = gateway(v)
    with pytest.raises(DomainError):
        if effect == "workspace":
            w["manager"].create(w["actor"], w["run"], w["token"])
        elif effect == "sandbox":
            o.controller.create(w["actor"], w["run"], "not-a-workspace", w["token"], "capture")
        elif effect == "qa":
            o.qa.run(w["actor"], w["run"], "bad", "acceptance", w["token"])
        elif effect == "verify":
            o.verify(w["actor"], w["run"], w["token"], "capture", "acceptance")
        elif effect == "quota":
            g.quota.reserve(w["actor"], w["run"], "bypass", w["token"], "prototype")
        else:
            o.store.objects.publish("bad", b"not authorized", "0" * 64)
    assert not v[3].calls and not v[2].calls
