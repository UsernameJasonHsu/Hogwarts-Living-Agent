from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from hogwarts.models import Proposal, PrototypeRun, WorkflowEvent
from hogwarts.recovery.contracts import deny
from hogwarts.recovery.executor import RecoveryExecutor
from hogwarts.recovery.inspector import WorkflowStateInspector
from hogwarts.recovery.planner import RecoveryPlanner
from hogwarts.recovery.queue import HumanInterventionQueue
from hogwarts.service import DomainError


class CrashRecoveryCoordinator:
    def __init__(self, orchestrator, *, backend=None, stale_seconds=60):
        self.inspector = WorkflowStateInspector(orchestrator, backend, stale_seconds)
        self.factory = orchestrator.factory
        self.interventions = HumanInterventionQueue(orchestrator.workspaces)
        self.planner = RecoveryPlanner(self.inspector, self.interventions)
        self.executor = RecoveryExecutor(self.planner)

    def _denied(self, actor, run_id, code):
        try:
            with self.factory.begin() as s:
                s.add(
                    WorkflowEvent(
                        owner_id=actor.owner_id,
                        aggregate_id=run_id,
                        aggregate_version=0,
                        source="workflow_recovery",
                        event_type="recovery.denied",
                        actor_id=actor.id,
                        reason="denied",
                        payload={"code": code},
                    )
                )
        except SQLAlchemyError:
            deny("recovery_database_and_audit_unavailable", 503)

    def recover(self, actor, run_id, key, lease_token=None, *, dry_run=True):
        if type(dry_run) is not bool:
            deny("recovery_invalid_dry_run", 403)
        try:
            j = self.planner.plan(actor, run_id, key, lease_token)
            return (
                j if dry_run else self.execute(actor, run_id, j.recovery_id, j.version, lease_token)
            )
        except SQLAlchemyError:
            deny("recovery_database_unavailable", 503)
        except DomainError as error:
            self._denied(actor, run_id, error.code)
            raise
        except (OSError, ValueError, KeyError, TypeError):
            self._denied(actor, run_id, "recovery_state_unavailable")
            deny("recovery_state_unavailable", 503)

    def execute(self, actor, run_id, recovery_id, expected_version, lease_token=None):
        try:
            return self.executor.execute(actor, run_id, recovery_id, expected_version, lease_token)
        except SQLAlchemyError:
            deny("recovery_database_unavailable", 503)
        except DomainError as error:
            self._denied(actor, run_id, error.code)
            raise
        except (OSError, ValueError, KeyError, TypeError):
            self._denied(actor, run_id, "recovery_state_unavailable")
            deny("recovery_state_unavailable", 503)

    def scan(self, actor, *, limit=50):
        if actor.role != "human" or type(limit) is not int or not 1 <= limit <= 100:
            deny("recovery_invalid_scan", 403)
        with self.factory() as s:
            ids = list(
                s.scalars(
                    select(PrototypeRun.id)
                    .join(Proposal, PrototypeRun.proposal_id == Proposal.id)
                    .where(Proposal.owner_id == actor.owner_id)
                    .order_by(PrototypeRun.id)
                    .limit(limit)
                )
            )
        reports = []
        for run_id in ids:
            with self.factory.begin() as s:
                obs, _ = self.inspector.inspect(s, actor, run_id)
            if obs.stalled and (
                obs.decision.classification == "MANUAL_REQUIRED" or obs.decision.action != "NONE"
            ):
                reports.append(self.recover(actor, run_id, "scan_" + obs.fingerprint[:40]))
        return reports
