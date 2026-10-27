"""DB transaction fences shared by existing execution/publication boundaries."""

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from hogwarts.models import PrototypeRun, now


def reject():
    from hogwarts.service import DomainError

    raise DomainError(409, "stale_or_expired_fence")


def fence(session, run, *, active=True):
    # A write predicate serializes with heartbeat/revocation on SQLite and PostgreSQL.
    # Never let an identity-map cache authorize against an obsolete DB row.
    generation, token = run.lease_generation, run.lease_token
    predicate = [
        PrototypeRun.id == run.id,
        PrototypeRun.lease_generation == generation,
        PrototypeRun.lease_generation == PrototypeRun.lease_issued_generation,
        PrototypeRun.lease_generation > 0,
        PrototypeRun.lease_token == token,
        PrototypeRun.lease_revoked_at.is_(None),
    ]
    if active:
        predicate += [PrototypeRun.lease_expires_at > now(), PrototypeRun.status == "CLAIMED"]
    with session.no_autoflush:
        changed = session.execute(
            update(PrototypeRun)
            .where(*predicate)
            .values(lease_version=PrototypeRun.lease_version)
            .execution_options(synchronize_session=False)
        )
    if changed.rowcount != 1:
        reject()
    guards = session.info.setdefault("lease_fences", {})
    prior = guards.get(run.id)
    if prior and prior[:2] != (generation, token):
        reject()
    guards[run.id] = (generation, token, active or bool(prior and prior[2]))
    with session.no_autoflush:
        session.refresh(run, attribute_names=["lease_expires_at", "lease_version"])


@event.listens_for(Session, "before_commit")
def check_fences(session):
    guards = session.info.get("lease_fences", {})
    if not guards:
        return
    for run_id, (generation, token, active) in guards.items():
        predicate = [
            PrototypeRun.id == run_id,
            PrototypeRun.lease_generation == generation,
            PrototypeRun.lease_generation == PrototypeRun.lease_issued_generation,
            PrototypeRun.lease_generation > 0,
            PrototypeRun.lease_token == token,
            PrototypeRun.lease_revoked_at.is_(None),
        ]
        if active:
            predicate.append(PrototypeRun.lease_expires_at > now())
        with session.no_autoflush:
            if (
                session.execute(
                    update(PrototypeRun)
                    .where(*predicate)
                    .values(lease_version=PrototypeRun.lease_version)
                    .execution_options(synchronize_session=False)
                ).rowcount
                != 1
            ):
                reject()


@event.listens_for(Session, "after_flush_postexec")
def check_flushed_fences(session, flush_context):
    # Validate flushed writes too, without changing before_commit observer semantics.
    check_fences(session)


def lock_run(session, run):
    """Cleanup/claim/cancel lock order only; does not grant execution authority."""
    with session.no_autoflush:
        session.execute(
            update(PrototypeRun)
            .where(PrototypeRun.id == run.id)
            .values(lease_version=PrototypeRun.lease_version)
            .execution_options(synchronize_session=False)
        )
        session.refresh(run)


@event.listens_for(Session, "after_commit")
@event.listens_for(Session, "after_rollback")
def clear_fences(session):
    session.info.pop("lease_fences", None)
