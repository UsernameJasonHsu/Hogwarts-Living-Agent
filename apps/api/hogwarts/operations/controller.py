"""Admission before deterministic verification; uncertain intent never replays."""

from contextlib import nullcontext
from dataclasses import asdict

from hogwarts.reconciliation.contracts import deny
from hogwarts.service import digest
from hogwarts.verification.contracts import VerificationInfo


class OperationalPrototypeController:
    def __init__(self, quota, orchestrator, *, execution_scope=None):
        if quota.factory is not orchestrator.factory:
            deny("quota_controller_binding_mismatch", 403)
        self.quota, self.orchestrator = quota, orchestrator
        self.execution_scope = execution_scope
        self.configurations = {}
        for name, profile in quota.profiles.items():
            runtime = orchestrator.controller.profiles.get(profile.runtime_profile)
            qa = orchestrator.qa.registry.get(profile.qa_profile)
            if not runtime or not qa:
                deny("quota_profile_not_approved", 403)
            # A conservative peak covers both approved workload allocations. This
            # does not assert a kernel disk limit or meter daemon overhead.
            d = profile.demand
            if (
                d.cpu_millis < round(1000 * (runtime.cpus + qa.cpus))
                or d.memory_bytes < (runtime.memory_mb + qa.memory_mb) * 2**20
                or d.pids < runtime.pids + qa.pids
            ):
                deny("quota_profile_underprovisioned", 403)
            self.configurations[name] = digest([asdict(runtime), asdict(qa)])

    def run(self, actor, run_id, request_key, token, profile_name):
        q, o = self.quota, self.orchestrator
        p = q.profiles.get(profile_name)
        if not p:
            deny("quota_profile_not_approved", 403)
        runtime, qa = o.controller.profiles.get(p.runtime_profile), o.qa.registry.get(p.qa_profile)
        if (
            not runtime
            or not qa
            or digest([asdict(runtime), asdict(qa)]) != self.configurations[profile_name]
        ):
            deny("quota_runtime_configuration_changed")
        receipt = q.receipt(actor, run_id, request_key, token)
        if receipt and receipt.profile_sha256 != digest(p.model_dump()):
            deny("quota_request_conflict")
        if receipt and receipt.state == "COMPLETED":
            return VerificationInfo.model_validate(receipt.result)
        if receipt and receipt.state != "RESERVED":
            deny("quota_execution_uncertain")
        r = q.reserve(actor, run_id, request_key, token, profile_name)
        q._transition(actor, run_id, r.reservation_id, token, "RESERVED", "IN_USE")
        try:
            with (
                self.execution_scope(actor, run_id, token, r)
                if self.execution_scope
                else nullcontext()
            ):
                result = o.verify(actor, run_id, token, p.runtime_profile, p.qa_profile)
        except Exception:
            # No retry is attempted, including when DB cannot save uncertainty.
            q._transition(actor, run_id, r.reservation_id, token, "IN_USE", "UNKNOWN")
            deny("quota_execution_uncertain")
        q._transition(actor, run_id, r.reservation_id, token, "IN_USE", "COMPLETED", result=result)
        return result
