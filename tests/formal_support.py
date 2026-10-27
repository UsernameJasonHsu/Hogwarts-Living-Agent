from hogwarts.entrypoints.gateway import FormalExecutionGateway
from hogwarts.operations.contracts import Capacity, QuotaProfile, VersionedRetentionPolicy
from hogwarts.operations.quota import AggregateResourceQuotaManager
from hogwarts.operations.storage import StorageUsageAccounting
from hogwarts.reconciliation.docker import DockerResourceBackend
from recovery_support import real_orchestrator
from test_verification import HUMAN


def real_formal(manager, root, controller_id, *, slots=2, executable=None):
    o = real_orchestrator(manager, root, controller_id, executable=executable)
    roots = FormalExecutionGateway.roots(o)
    quota = AggregateResourceQuotaManager(
        manager,
        {
            HUMAN.owner_id: Capacity(
                slots=slots, cpu_millis=4000, memory_bytes=2**31, pids=512, storage_bytes=2**30
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
        StorageUsageAccounting({HUMAN.owner_id: roots}, min_free_bytes=0),
        backend=DockerResourceBackend(o.controller, o.qa),
    )
    return FormalExecutionGateway(
        quota,
        o,
        "prototype",
        retention_policy=VersionedRetentionPolicy(name="formal-fixture", ephemeral_ttl_seconds=0),
    ), o
