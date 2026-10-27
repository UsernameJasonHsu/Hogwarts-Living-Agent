"""No execution inferred from a process disappearing; only authenticated facts."""

from hogwarts.recovery.contracts import Decision


class RecoveryClassifier:
    @staticmethod
    def classify(facts, actor):
        def manual(reason):
            return Decision(classification="MANUAL_REQUIRED", reason=reason)

        if facts["problem"]:
            return manual(facts["problem"])
        if not facts["budget"]["valid"]:
            return manual("recovery_budget_counters_inconsistent")
        if facts["cancel_normalize"]:
            if facts["resources_present"] or facts["budget"]["status"] != "RELEASED":
                return manual("recovery_cancel_cleanup_or_accounting_required")
            return Decision(
                classification="FINALIZE",
                action="CANCEL_METADATA",
                reason="recovery_human_cancel_confirmed",
            )
        if facts["terminal"]:
            if facts["stale_approval"]:
                return manual("recovery_stale_human_approval")
            if facts["budget"]["status"] == "RELEASED" and facts["budget"]["actual"] == 0:
                return Decision(classification="FINALIZE", reason="recovery_legal_terminal")
            if (
                facts["budget"]["status"] == "RESERVED"
                and facts["cost_proof"]
                and facts["budget"]["actual"] is None
                and not facts["resources_present"]
            ):
                return Decision(
                    classification="FINALIZE",
                    action="SETTLE_BUDGET",
                    reason="recovery_deterministic_cost_proven",
                )
            return manual("recovery_cost_evidence_insufficient")
        if facts["unknown_execution"]:
            return manual("recovery_unknown_execution")
        if not facts["active"]:
            return manual("recovery_inactive_lease")
        if facts["candidate"]:
            if actor.role != "worker":
                return manual("recovery_original_worker_required")
            action, target = facts["candidate"]
            return Decision(
                classification="SAFE_RESUME",
                action=action,
                target_id=target,
                reason="recovery_complete_publication_proof",
            )
        return manual("recovery_no_safe_publication_proof")
