"""Run-first, owner-aggregate durable commitments, independent of money budgets."""

from types import MappingProxyType
from uuid import uuid4

from sqlalchemy import select

from hogwarts.models import Proposal, PrototypeRun, SandboxInstance
from hogwarts.operations.contracts import Capacity, QuotaLedger, QuotaReservation
from hogwarts.operations.state import OperationalState, guarded, key
from hogwarts.reconciliation.contracts import deny
from hogwarts.reconciliation.inventory import ResourceInventory
from hogwarts.service import digest

LEDGER = "quota-ledger.v1"


class AggregateResourceQuotaManager(OperationalState):
    def __init__(self, workspaces, limits, profiles, storage, *, backend=None):
        super().__init__(workspaces)
        if not limits or any(type(x) is not Capacity for x in limits.values()):
            deny("quota_invalid_limits", 403)
        if any(any(v <= 0 for v in x.model_dump().values()) for x in limits.values()):
            deny("quota_invalid_limits", 403)
        self.limits = MappingProxyType(dict(limits))
        self.profiles = MappingProxyType({p.name: p for p in profiles})
        if len(self.profiles) != len(profiles) or any(
            any(v <= 0 for v in p.demand.model_dump().values()) for p in profiles
        ):
            deny("quota_invalid_profiles", 403)
        self.storage = storage
        for roots in storage.roots.values():
            for root in roots:
                if any(
                    root == g.repository
                    or root in g.repository.parents
                    or g.repository in root.parents
                    for g in workspaces.grants.values()
                ):
                    deny("storage_source_repository_forbidden", 403)
        self.inventory = ResourceInventory(workspaces, backend)

    def ledger(self, s, actor, *, lock=True):
        row = self.find(s, actor, LEDGER, lock=lock)
        limit = self.limits.get(actor.owner_id)
        if not row or not limit:
            deny("quota_not_initialized", 403)
        ledger = self.value(row, QuotaLedger, actor.owner_id)
        if ledger.limits_sha256 != digest(limit.model_dump()):
            deny("quota_configuration_changed")
        if len({r.reservation_id for r in ledger.reservations}) != len(ledger.reservations):
            deny("quota_ambiguous_ledger")
        if len({(r.actor_id, r.request_key) for r in ledger.reservations}) != len(
            ledger.reservations
        ):
            deny("quota_ambiguous_ledger")
        ids = {r.run_id for r in ledger.reservations}
        runs = (
            {
                run.id: (run, proposal)
                for run, proposal in s.execute(
                    select(PrototypeRun, Proposal)
                    .join(Proposal, PrototypeRun.proposal_id == Proposal.id)
                    .where(PrototypeRun.id.in_(ids))
                )
            }
            if ids
            else {}
        )
        for r in ledger.reservations:
            profile = self.profiles.get(r.profile)
            binding = runs.get(r.run_id)
            if (
                not profile
                or not binding
                or r.demand != profile.demand
                or r.profile_sha256 != digest(profile.model_dump())
                or binding[1].owner_id != actor.owner_id
                or binding[1].project_id != r.project_id
                or binding[0].worker_id != r.actor_id
                or r.generation != binding[0].lease_issued_generation
                or r.generation > binding[0].lease_generation
                or (r.state == "COMPLETED" and r.result is None)
            ):
                deny("quota_ledger_state_invalid")
        return row, ledger

    @guarded
    def initialize(self, actor, run_id):
        with self.factory.begin() as s:
            self.context(s, actor, run_id, admin=True)
            limit = self.limits.get(actor.owner_id)
            if not limit:
                deny("quota_owner_not_configured", 403)
            row = self.find(s, actor, LEDGER, lock=True)
            if row:
                return self.ledger(s, actor)[1]
            value = QuotaLedger(owner_id=actor.owner_id, limits_sha256=digest(limit.model_dump()))
            self.save(s, actor, LEDGER, value)
            self.event(s, actor, run_id, "quota_initialized", version=value.version)
            return value

    @staticmethod
    def total(ledger):
        values = {k: 0 for k in Capacity.model_fields}
        for r in ledger.reservations:
            if r.state != "RELEASED":
                for k, v in r.demand.model_dump().items():
                    values[k] += v
        return Capacity(**values)

    def replace(self, s, actor, run_id, row, ledger, reservation):
        values = tuple(
            reservation if r.reservation_id == reservation.reservation_id else r
            for r in ledger.reservations
        )
        if reservation.reservation_id not in {r.reservation_id for r in values}:
            values += (reservation,)
        new = ledger.model_copy(update={"version": ledger.version + 1, "reservations": values})
        self.save(s, actor, LEDGER, new, row)
        self.event(
            s,
            actor,
            run_id,
            "quota_" + reservation.state.lower(),
            version=new.version,
            reservation_id=reservation.reservation_id,
            generation=reservation.generation,
        )
        return reservation

    @guarded
    def reserve(self, actor, run_id, request_key, token, profile):
        key(request_key)
        approved = self.profiles.get(profile)
        if not approved:
            deny("quota_profile_not_approved", 403)
        with self.factory.begin() as s:
            run, proposal, project = self.context(s, actor, run_id, token, live=True)
            row, ledger = self.ledger(s, actor)
            for r in ledger.reservations:
                if r.actor_id == actor.id and r.request_key == request_key:
                    if (
                        r.run_id != run_id
                        or r.generation != run.lease_generation
                        or r.profile_sha256 != digest(approved.model_dump())
                    ):
                        deny("quota_request_conflict")
                    if r.state == "RELEASED":
                        deny("quota_reservation_released")
                    return r
            if len(ledger.reservations) >= 1000:
                deny("quota_ledger_capacity")
            # A legacy unreserved runtime must not silently disappear from accounting.
            held_runs = {r.run_id for r in ledger.reservations if r.state != "RELEASED"}
            legacy = s.scalars(
                select(SandboxInstance)
                .join(PrototypeRun, SandboxInstance.run_id == PrototypeRun.id)
                .join(Proposal, PrototypeRun.proposal_id == Proposal.id)
                .where(Proposal.owner_id == actor.owner_id, SandboxInstance.state != "DESTROYED")
            )
            if any(x.run_id not in held_runs for x in legacy):
                deny("quota_unaccounted_runtime")
            _, resources = self.inventory.inspect(s, run, proposal, project)
            if any(x.disposition == "QUARANTINE" for x in resources):
                deny("quota_resource_state_unknown")
            usage = self.storage.measure(s, actor.owner_id)
            total = self.total(ledger).model_dump()
            demand, limit = approved.demand.model_dump(), self.limits[actor.owner_id].model_dump()
            prospective = {k: total[k] + demand[k] for k in total}
            if any(
                v
                + (
                    usage.logical_bytes + usage.unmaterialized_artifact_bytes
                    if k == "storage_bytes"
                    else 0
                )
                > limit[k]
                for k, v in prospective.items()
            ):
                deny("quota_capacity_exceeded")
            if (
                usage.free_bytes_min
                < self.storage.min_free_bytes
                + prospective["storage_bytes"]
                + usage.unmaterialized_artifact_bytes
            ):
                deny("quota_disk_headroom_exhausted")
            result = QuotaReservation(
                reservation_id=str(uuid4()),
                run_id=run_id,
                project_id=project.id,
                actor_id=actor.id,
                generation=run.lease_generation,
                profile=profile,
                profile_sha256=digest(approved.model_dump()),
                request_key=request_key,
                demand=approved.demand,
            )
            return self.replace(s, actor, run_id, row, ledger, result)

    def reservation(self, ledger, actor, run_id, identifier):
        r = next((r for r in ledger.reservations if r.reservation_id == identifier), None)
        if not r or r.run_id != run_id or (actor.role != "human" and r.actor_id != actor.id):
            deny("quota_reservation_not_found", 404)
        return r

    @guarded
    def _transition(self, actor, run_id, identifier, token, expected, state, *, result=None):
        if expected not in {"RESERVED", "IN_USE"} or state not in {
            "IN_USE",
            "COMPLETED",
            "UNKNOWN",
        }:
            deny("quota_invalid_transition", 403)
        if (expected, state) not in {
            ("RESERVED", "IN_USE"),
            ("IN_USE", "COMPLETED"),
            ("IN_USE", "UNKNOWN"),
        }:
            deny("quota_invalid_transition", 403)
        with self.factory.begin() as s:
            run, _, _ = self.context(s, actor, run_id, token, live=state == "IN_USE")
            row, ledger = self.ledger(s, actor)
            r = self.reservation(ledger, actor, run_id, identifier)
            if r.generation != run.lease_generation:
                deny("quota_generation_changed")
            if r.state != expected:
                deny("quota_cas_conflict")
            if state == "COMPLETED":
                from hogwarts.verification.contracts import VerificationInfo

                if (
                    not isinstance(result, VerificationInfo)
                    or result.run_id != run_id
                    or result.status != run.status
                    or (run.evidence or {}).get("verification_id") != result.verification_id
                    or (run.evidence or {}).get("bundle_sha256") != result.bundle_sha256
                ):
                    deny("quota_publisher_receipt_invalid")
                result = result.model_dump(mode="json")
            return self.replace(
                s,
                actor,
                run_id,
                row,
                ledger,
                r.model_copy(update={"state": state, "result": result}),
            )

    @guarded
    def receipt(self, actor, run_id, request_key, token):
        key(request_key)
        with self.factory.begin() as s:
            run, _, _ = self.context(s, actor, run_id, token)
            _, ledger = self.ledger(s, actor)
            r = next(
                (
                    r
                    for r in ledger.reservations
                    if r.actor_id == actor.id and r.request_key == request_key
                ),
                None,
            )
            if r and (r.run_id != run_id or r.generation != run.lease_generation):
                deny("quota_request_conflict")
            return r

    @guarded
    def release(self, actor, run_id, identifier, expected_version):
        with self.factory.begin() as s:
            run, proposal, project = self.context(s, actor, run_id, admin=True)
            row, ledger = self.ledger(s, actor)
            r = self.reservation(ledger, actor, run_id, identifier)
            if r.state == "RELEASED" and type(expected_version) is int and expected_version > 0:
                return r
            if type(expected_version) is not int or ledger.version != expected_version:
                deny("quota_cas_conflict")
            from hogwarts.reconciliation.identity import StateComparator

            if StateComparator.active(run):
                deny("quota_active_lease")
            _, resources = self.inventory.inspect(s, run, proposal, project)
            if any(x.kind != "evidence" for x in resources):
                deny("quota_resources_not_absent")
            # Retained evidence is measured, never deleted to release capacity.
            self.storage.measure(s, actor.owner_id)
            return self.replace(
                s, actor, run_id, row, ledger, r.model_copy(update={"state": "RELEASED"})
            )
