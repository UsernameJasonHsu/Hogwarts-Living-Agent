"""Evidence identity, immutable content and authenticated execution checks."""

import hashlib

from hogwarts.models import GitWorkspace, IdempotencyRecord
from hogwarts.outputs.capsule import canonical
from hogwarts.qa.contracts import QAExecutionAttestation, deny
from hogwarts.qa.driver import container_argv
from hogwarts.service import digest
from hogwarts.verification.contracts import EvidenceRef, ReviewBundle
from hogwarts.verification.profiles import approved_review_profile


class AcceptanceGate:
    def __init__(self, qa):
        self.qa, self.store, self.factory = qa, qa.store, qa.factory

    def evaluate(self, actor, run_id, token, verification_id, built, result):
        qa = self.qa
        with self.factory() as s:
            row = s.get(IdempotencyRecord, result.qa_id)
            if not row or row.owner_id != actor.owner_id or row.actor_id != actor.id:
                deny("verification_qa_not_authorized", 403)
            profile = approved_review_profile(qa.registry.get(row.response["profile_name"]))
        inputs = qa.validator.load(actor, run_id, built.build_id, token, profile)
        c, build = inputs.context, inputs.build
        with self.factory() as s:
            row = s.get(IdempotencyRecord, result.qa_id)
            j = qa._journal(row, inputs, profile, actor, token)
            if (
                j.state != "READY"
                or row.key
                != "qa-runner.v1:" + digest({"build": build.build_id, "profile": profile.name})
                or j.attestation_artifact_id != result.attestation_artifact_id
                or j.attestation_sha256 != result.attestation_sha256
            ):
                deny("verification_qa_not_ready")
        _, observed = qa._result(j)
        att_obj = self.store.read(actor, run_id, j.attestation_artifact_id, token)
        att = QAExecutionAttestation.model_validate_json(att_obj.content)
        expected = {
            "qa_id": j.qa_id,
            "run_id": run_id,
            "worker_id": actor.id,
            "controller_id": qa.driver.controller_id,
            "workspace_id": c.workspace_id,
            "original_sandbox_id": c.sandbox_id,
            "qa_sandbox_id": j.qa_id,
            "build_id": build.build_id,
            "patch_artifact_id": build.patch_artifact_id,
            "patch_sha256": build.patch_sha256,
            "build_proof_artifact_id": build.proof_artifact_id,
            "build_proof_sha256": build.proof_sha256,
            "base_commit": c.base_commit,
            "result_tree": build.proof.result_tree,
            "profile_name": profile.name,
            "profile_sha256": profile.fingerprint,
            "argv": list(profile.argv),
            "container_argv": container_argv(qa._resource(j), profile),
            "image_id": profile.image,
            "input_sha256": hashlib.sha256(inputs.archive).hexdigest(),
            **{
                k: getattr(observed, k)
                for k in (
                    "outcome",
                    "exit_code",
                    "container_id",
                    "started_at",
                    "finished_at",
                    "oom_killed",
                    "timed_out",
                    "cleanup_complete",
                    "error_code",
                )
            },
        }
        if (
            any(getattr(att, k) != v for k, v in expected.items())
            or canonical(att.model_dump(mode="json")) != att_obj.content
            or hashlib.sha256(att_obj.content).hexdigest() != j.attestation_sha256
            or j.outcome != observed.outcome
            or result.outcome != observed.outcome
            or att.resource_limits
            != {
                "cpus": profile.cpus,
                "memory_mb": profile.memory_mb,
                "pids": profile.pids,
                "timeout_seconds": profile.timeout_seconds,
                "absolute_expires_at": j.expires_at,
                "uid": 10001,
                "gid": 10001,
                "network": "none",
                "rootfs": "readonly",
                "capabilities": [],
                "no_new_privileges": True,
                "input": "readonly managed tmpfs volume",
                "output_bytes": 16777216,
                "tmp_bytes": 8388608,
            }
        ):
            deny("verification_attestation_mismatch")
        refs = {}

        def read(identifier, expected_hash=None):
            obj = self.store.read(actor, run_id, identifier, token)
            meta = obj.evidence.artifact
            if (
                meta.run_id != run_id
                or meta.workspace_id != c.workspace_id
                or meta.sandbox_id != c.sandbox_id
                or meta.base_commit != c.base_commit
                or meta.producer_id != actor.id
                or obj.evidence.assessment.status != "unverified"
                or (expected_hash and meta.sha256 != expected_hash)
            ):
                deny("verification_artifact_binding_mismatch")
            refs[identifier] = EvidenceRef(
                artifact_id=identifier,
                sha256=meta.sha256,
                size_bytes=meta.size_bytes,
            )
            return obj.content

        read(c.manifest_id, c.manifest_sha256)
        read(c.archive_id, c.archive_sha256)
        for file in c.files:
            read(file["artifact_id"], file["sha256"])
        read(build.patch_artifact_id, build.patch_sha256)
        read(build.proof_artifact_id, build.proof_sha256)
        read(j.attestation_artifact_id, j.attestation_sha256)
        for ref, raw in ((att.stdout, observed.stdout), (att.stderr, observed.stderr)):
            data = read(ref.artifact_id, ref.sha256)
            if data != bytes.fromhex(raw) or len(data) != ref.size_bytes:
                deny("verification_log_mismatch")
        verdict, reasons, count = "READY_FOR_HUMAN_REVIEW", [], 2
        if observed.outcome == "unknown_execution":
            verdict, reasons, count = "UNKNOWN", ["unknown_qa_execution"], 0
        elif observed.exit_code == 75:
            verdict, reasons, count = "BLOCKED", ["zero_or_insufficient_tests"], 0
        elif observed.outcome != "pass" or observed.exit_code != 0:
            verdict, reasons, count = "FAILED", ["qa_nonpassing"], 0
        elif not observed.cleanup_complete:
            verdict, reasons, count = "BLOCKED", ["qa_cleanup_incomplete"], 0
        elif build.proof.applicability != "applicable" or not build.proof.changed_paths:
            verdict, reasons, count = "BLOCKED", ["empty_patch"], 0
        with self.factory() as s:
            _, proposal, _ = qa.workspaces._authorize(s, actor, run_id, token)
            ws = s.get(GitWorkspace, c.workspace_id)
            from hogwarts.models import now

            return ReviewBundle(
                verification_id=verification_id,
                run_id=run_id,
                workspace_id=c.workspace_id,
                sandbox_id=c.sandbox_id,
                worker_id=actor.id,
                base_commit=c.base_commit,
                configuration_sha256=ws.request_hash,
                proposal_sha256=digest(proposal.document),
                profile_name=profile.name,
                profile_sha256=profile.fingerprint,
                image_id=profile.image,
                build_id=build.build_id,
                qa_id=j.qa_id,
                attestation_artifact_id=j.attestation_artifact_id,
                patch_artifact_id=build.patch_artifact_id,
                build_proof_artifact_id=build.proof_artifact_id,
                input_sha256=j.input_sha256,
                result_tree=build.proof.result_tree,
                changed_paths=build.proof.changed_paths,
                verdict=verdict,
                reasons=reasons,
                tests_executed=count,
                test_count_authority="approved_image_exit_protocol"
                if count
                else "insufficient_evidence",
                artifacts=sorted(refs.values(), key=lambda ref: ref.artifact_id),
                created_at=now().isoformat(),
            )
