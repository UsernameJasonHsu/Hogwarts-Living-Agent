"""Fail-closed classification; labels alone never establish resource ownership."""

from hogwarts.models import now
from hogwarts.service import utc


class StateComparator:
    @staticmethod
    def active(run):
        return (
            run.lease_revoked_at is None
            and run.lease_expires_at is not None
            and utc(run.lease_expires_at) > now()
        )

    @classmethod
    def classify(cls, run, resources):
        valid = bool(
            run.lease_token
            and run.lease_issued_generation > 0
            and run.lease_generation >= run.lease_issued_generation
            and (
                run.lease_generation == run.lease_issued_generation
                or run.lease_revoked_at is not None
            )
        )
        result = []
        for resource in resources:
            if resource.disposition == "QUARANTINE" or resource.kind == "evidence":
                result.append(resource)
            elif not valid:
                result.append(
                    resource.model_copy(
                        update={"disposition": "QUARANTINE", "reason": "invalid_lease_issuance"}
                    )
                )
            elif cls.active(run):
                result.append(
                    resource.model_copy(update={"disposition": "KEEP", "reason": "active_lease"})
                )
            elif run.lease_expires_at is None and run.lease_revoked_at is None:
                result.append(
                    resource.model_copy(
                        update={"disposition": "QUARANTINE", "reason": "unknown_lease_expiry"}
                    )
                )
            else:
                result.append(resource)
        return tuple(sorted(result, key=lambda x: x.key))


class IdentityVerifier:
    @staticmethod
    def unchanged(plan, binding, resources):
        return plan.binding_sha256 == binding and plan.resources == resources

    @staticmethod
    def removable(resource):
        return resource.disposition == "CLEANUP_ELIGIBLE" and resource.kind in {
            "container",
            "volume",
            "worktree",
        }
