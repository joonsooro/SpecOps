"""Deterministic provider-free Artifact Quality Audit fixtures."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid5

from specops_contracts import artifact_quality_v1 as q
from specops_workshop.v4.artifact_quality_adapter import (
    ArtifactQualityEvaluation,
    PreparedArtifactQualityContext,
)


NAMESPACE = UUID("6ac0cd95-6579-4ae6-87b0-71fc08ec93c4")


def stable_id(*parts: object) -> UUID:
    return uuid5(NAMESPACE, ":".join(str(item) for item in parts))


def all_pass_candidate(
    bundle: q.ArtifactQualityAuditBundle,
) -> q.ArtifactSemanticAttestationCandidate:
    return q.ArtifactSemanticAttestationCandidate(
        protocol_version=q.PROTOCOL_VERSION,
        output_type="ARTIFACT_SEMANTIC_ATTESTATION_CANDIDATE",
        audit_id=bundle.audit_id,
        evaluator_run_id=bundle.evaluator_run_id,
        request_hash=bundle.request_hash,
        artifact_id=bundle.subject.artifact_id,
        artifact_version=bundle.subject.artifact_version,
        record_revision=bundle.subject.record_revision,
        payload_hash=bundle.subject.payload_hash,
        audit_scope_manifest_hash=bundle.audit_scope_manifest_hash,
        semantic_quality_contract_hash=bundle.quality_contract.content_hash,
        assessments=tuple(
            q.SemanticRuleAssessment(
                rule_id=rule_id,
                result=q.SemanticAssessmentResult.PASS,
                explanation="The complete audit universe supports this semantic requirement.",
                applicability_reason=None,
                artifact_pointers=("",),
                evidence_ids=(),
                transcript_event_ids=(),
                findings=(),
            )
            for rule_id in bundle.semantic_rule_ids
        ),
    )


def admit_all_pass_quality(foundation, bundle):
    prepared = foundation.prepare_artifact_quality_audit(
        q.PrepareArtifactQualityAuditCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=stable_id(bundle.audit_id, "prepare"),
            idempotency_key=f"test-quality-prepare-{bundle.audit_id}",
            expected_case_revision=bundle.based_on_case_revision,
            bundle=bundle,
        )
    )
    assert prepared.existing_receipt is None
    started = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
    conversation_id = f"conv_quality_{str(bundle.audit_id).replace('-', '')[:20]}"
    foundation.bind_artifact_quality_evaluator(
        q.BindArtifactQualityEvaluatorCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=stable_id(bundle.audit_id, "bind"),
            audit_id=bundle.audit_id,
            request_hash=bundle.request_hash,
            provider="OPENAI",
            model="gpt-5.6-terra",
            reasoning_effort="medium",
            provider_conversation_id=conversation_id,
            client_request_id=f"aqa-context-{bundle.request_hash[7:23]}",
            started_at=started,
        )
    )
    candidate = all_pass_candidate(bundle)
    return foundation.admit_artifact_quality_audit(
        q.AdmitArtifactQualityAuditCommand(
            protocol_version=q.PROTOCOL_VERSION,
            command_id=stable_id(bundle.audit_id, "admit"),
            idempotency_key=f"test-quality-admit-{bundle.audit_id}",
            expected_case_revision=bundle.based_on_case_revision,
            bundle=bundle,
            execution=q.EvaluatorExecutionBinding(
                provider="OPENAI",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                provider_conversation_id=conversation_id,
                provider_response_id=f"resp_quality_{bundle.request_hash[7:23]}",
                client_request_id=f"aqa-response-{bundle.request_hash[7:23]}",
                store_enabled=True,
                started_at=started,
                completed_at=started,
            ),
            candidate=candidate,
        )
    )


class DeterministicQualityEvaluator:
    def __init__(self, *, conversation_id: str = "conv_quality_task26") -> None:
        self.conversation_id = conversation_id
        self.prepare_calls = 0
        self.evaluate_calls = 0
        self.released: list[str] = []
        self.bundles: list[q.ArtifactQualityAuditBundle] = []

    async def prepare(self, bundle, *, prohibited_conversation_id):
        assert self.conversation_id != prohibited_conversation_id
        self.prepare_calls += 1
        self.bundles.append(bundle)
        return PreparedArtifactQualityContext(
            provider="OPENAI",
            model="gpt-5.6-terra",
            reasoning_effort="medium",
            provider_conversation_id=self.conversation_id,
            client_request_id=f"aqa-context-{bundle.request_hash[7:23]}",
            started_at=datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc),
        )

    async def evaluate(self, bundle, *, prepared):
        self.evaluate_calls += 1
        started = prepared.started_at
        return ArtifactQualityEvaluation(
            execution=q.EvaluatorExecutionBinding(
                provider="OPENAI",
                model="gpt-5.6-terra",
                reasoning_effort="medium",
                provider_conversation_id=prepared.provider_conversation_id,
                provider_response_id=f"resp_quality_{bundle.request_hash[7:23]}",
                client_request_id=f"aqa-response-{bundle.request_hash[7:23]}",
                store_enabled=True,
                started_at=started,
                completed_at=started,
            ),
            candidate=all_pass_candidate(bundle),
        )

    async def release(self, prepared):
        self.released.append(prepared.provider_conversation_id)
