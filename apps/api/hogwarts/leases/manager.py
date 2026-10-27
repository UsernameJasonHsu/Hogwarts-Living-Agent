"""Internal controller API. Authority comes from M1, never from agent text."""

import re
import secrets
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError

from hogwarts.leases.contracts import Heartbeat, LeaseInfo
from hogwarts.leases.fencing import fence
from hogwarts.models import PrototypeRun, WorkflowEvent, now
from hogwarts.service import DomainError, command, proposal_for, utc


def emit(s, actor, run, event, code=None):
    s.add(
        WorkflowEvent(
            owner_id=actor.owner_id,
            aggregate_id=run.id,
            aggregate_version=run.lease_version,
            event_type="lease." + event,
            source="lease_controller",
            actor_id=actor.id,
            reason=event,
            payload={
                "generation": run.lease_generation,
                "version": run.lease_version,
                "code": code,
            },
        )
    )


def invalidate(s, actor, run, reason):
    if run.lease_revoked_at is None:
        run.lease_generation += 1
        run.lease_version += 1
        run.lease_revoked_at = now()
        run.lease_revocation_reason = reason
        run.lease_stop_state = "PENDING"
        emit(s, actor, run, "revoked", reason)


class DurableLeaseManager:
    def __init__(self, factory, *, ttl_seconds=300):
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise ValueError("invalid lease TTL")
        self.factory, self.ttl_seconds = factory, ttl_seconds

    def _info(self, r):
        return LeaseInfo(
            run_id=r.id,
            generation=r.lease_generation,
            version=r.lease_version,
            expires_at=utc(r.lease_expires_at) if r.lease_expires_at else None,
            revoked=r.lease_revoked_at is not None,
            stop_state=r.lease_stop_state,
        )

    def _run(self, s, actor, run_id, *, human=False):
        if actor.role != ("human" if human else "worker"):
            raise DomainError(403, "human_required" if human else "worker_required")
        r = s.get(PrototypeRun, run_id)
        if not r:
            raise DomainError(404, "not_found")
        p = proposal_for(s, r.proposal_id, actor)
        return r, p

    def _guard(self, actor, run_id, fn):
        try:
            return fn()
        except DomainError as error:
            try:
                with self.factory.begin() as s:
                    s.add(
                        WorkflowEvent(
                            owner_id=actor.owner_id,
                            aggregate_id=run_id,
                            aggregate_version=0,
                            event_type="lease.denied",
                            source="lease_controller",
                            actor_id=actor.id,
                            reason="denied",
                            payload={"code": error.code},
                        )
                    )
            except SQLAlchemyError:
                raise DomainError(503, "lease_database_unavailable") from None
            raise
        except SQLAlchemyError:
            raise DomainError(503, "lease_database_unavailable") from None

    def _key(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,70}", key):
            raise DomainError(422, "invalid_lease_idempotency_key")
        return "lease.v1:" + key

    def heartbeat(self, actor, run_id, data: Heartbeat, key):
        def perform():
            command_key = self._key(key)
            # Revalidate on cached requests; an old response never resurrects authority.
            with self.factory() as s:
                r, p = self._run(s, actor, run_id)
                self._active(r, p, actor, data)

            def beat(s):
                r, p = self._run(s, actor, run_id)
                self._active(r, p, actor, data)
                stamp = now()
                result = s.execute(
                    update(PrototypeRun)
                    .where(
                        PrototypeRun.id == run_id,
                        PrototypeRun.lease_generation == data.generation,
                        PrototypeRun.lease_version == data.expected_version,
                        PrototypeRun.lease_revoked_at.is_(None),
                        PrototypeRun.lease_expires_at > stamp,
                        PrototypeRun.worker_id == actor.id,
                        PrototypeRun.lease_token == data.lease_token,
                        PrototypeRun.status == "CLAIMED",
                    )
                    .values(
                        lease_version=PrototypeRun.lease_version + 1,
                        lease_expires_at=stamp + timedelta(seconds=self.ttl_seconds),
                        lease_heartbeat_at=stamp,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise DomainError(409, "lease_cas_conflict")
                s.refresh(r)
                fence(s, r)
                emit(s, actor, r, "heartbeat")
                return self._info(r).model_dump(mode="json")

            result = command(
                self.factory, actor, command_key, "lease.heartbeat", run_id, data.model_dump(), beat
            )
            return LeaseInfo.model_validate(result)

        return self._guard(actor, run_id, perform)

    def _active(self, r, p, actor, data):
        if (
            r.worker_id != actor.id
            or not r.lease_token
            or not secrets.compare_digest(r.lease_token.encode(), data.lease_token.encode())
            or r.lease_generation != r.lease_issued_generation
            or r.lease_generation != data.generation
            or r.lease_revoked_at is not None
            or r.status != "CLAIMED"
            or p.status != "QUEUED"
            or not r.lease_expires_at
            or utc(r.lease_expires_at) <= now()
        ):
            raise DomainError(409, "inactive_lease")

    def revoke(self, actor, run_id, generation, key):
        return self._invalidate(actor, run_id, generation, key, False)

    def expire(self, actor, run_id, generation, key):
        return self._invalidate(actor, run_id, generation, key, True)

    def _invalidate(self, actor, run_id, generation, key, expired):
        def perform():
            if type(generation) is not int or generation < 0:
                raise DomainError(422, "invalid_generation")

            def mutate(s):
                r, _ = self._run(s, actor, run_id, human=True)
                # Acquire lock before refreshing/checking generation and wall clock.
                s.execute(
                    update(PrototypeRun)
                    .where(PrototypeRun.id == run_id)
                    .values(lease_version=PrototypeRun.lease_version)
                )
                s.refresh(r)
                if r.lease_generation != generation:
                    raise DomainError(409, "stale_fencing_token")
                if expired and (not r.lease_expires_at or utc(r.lease_expires_at) > now()):
                    raise DomainError(409, "lease_not_expired")
                invalidate(s, actor, r, "expired" if expired else "human_revoked")
                return self._info(r).model_dump(mode="json")

            # Authenticate before M1 replay.
            with self.factory() as s:
                self._run(s, actor, run_id, human=True)
            result = command(
                self.factory,
                actor,
                self._key(key),
                "lease.expire" if expired else "lease.revoke",
                run_id,
                {"generation": generation},
                mutate,
            )
            return LeaseInfo.model_validate(result)

        return self._guard(actor, run_id, perform)

    def stop(self, actor, run_id, hook):
        """Trusted idempotent stop callback only; no command/profile input from agents."""

        def perform():
            with self.factory.begin() as s:
                r, _ = self._run(s, actor, run_id, human=True)
                s.execute(
                    update(PrototypeRun)
                    .where(PrototypeRun.id == run_id)
                    .values(lease_version=PrototypeRun.lease_version)
                )
                s.refresh(r)
                if r.lease_revoked_at is None:
                    raise DomainError(409, "lease_not_revoked")
                if r.lease_stop_state == "CONFIRMED":
                    return self._info(r)
                r.lease_stop_state = "PENDING"
                emit(s, actor, r, "stop_requested")
            code = None
            try:
                hook()
            except Exception:
                code = "lease_stop_failed"
            with self.factory.begin() as s:
                r, _ = self._run(s, actor, run_id, human=True)
                s.execute(
                    update(PrototypeRun)
                    .where(PrototypeRun.id == run_id)
                    .values(lease_version=PrototypeRun.lease_version)
                )
                s.refresh(r)
                if r.lease_stop_state == "CONFIRMED":
                    return self._info(r)
                r.lease_stop_state = "FAILED" if code else "CONFIRMED"
                emit(s, actor, r, "stop_failed" if code else "stopped", code)
                return self._info(r)

        return self._guard(actor, run_id, perform)
