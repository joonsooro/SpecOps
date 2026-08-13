"""Deterministic production seam between Voice/HTTP, OpenAI, and Foundation.

Only this module is allowed to call the stored-Conversation adapter.  Provider
outputs are always proposals and are admitted through the Workshop Protocol
Foundation commands before becoming observable application state.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from specops_contracts import artifact_quality_v1 as q
from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import analyzer_request_hash, canonical_bytes, domain_hash, transcript_hash
from specops_workflow.workshop_protocol import WorkshopFoundationService

from .openai_adapter import ProviderSourceUpload, StoredConversationOpenAIAdapter
from .artifact_quality import build_audit_bundle
from .artifact_quality_adapter import (
    ArtifactQualityEvaluator,
    PreparedArtifactQualityContext,
)


PRODUCTION_NAMESPACE = UUID("e52d201a-e1d0-4df7-90e5-39ac4091998d")
ZERO_HASH = "sha256:" + "0" * 64


def _stable_id(*parts: object) -> UUID:
    return uuid5(PRODUCTION_NAMESPACE, ":".join(str(part) for part in parts))


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _request(model, values: dict[str, Any]):
    material = dict(values, request_hash=ZERO_HASH)
    material["request_hash"] = analyzer_request_hash(material)
    return model.model_validate(material)


class FinalTranscriptInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    turn_sequence: int = Field(strict=True, ge=1)
    text: str = Field(strict=True, min_length=1, max_length=100_000)
    provider_request_id: str = Field(strict=True, min_length=1, max_length=256)
    speaker_actor_id: UUID
    actor: Literal["PM", "DEV_LEAD", "OTHER_PARTICIPANT"] = "PM"


class TranscriptAnalysisReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    transcript: c.TranscriptRecordedReceipt
    analysis: c.ProposalAdmissionReceipt | None
    duplicate: bool


@dataclass(frozen=True)
class ProviderOperationAdmission:
    candidate: c.AnalyzerProviderCandidate
    receipt: c.FoundationReceipt
    quality_audit: q.ArtifactQualityAuditReceipt | None = None


class V4ProductionOrchestrator:
    """One restart-safe application path for all six provider operations."""

    def __init__(
        self,
        *,
        foundation: WorkshopFoundationService,
        adapter: StoredConversationOpenAIAdapter,
        case_id: UUID,
        session_id: UUID,
        sources: tuple[ProviderSourceUpload, ProviderSourceUpload],
        analyzer_contract: c.AnalyzerContractBinding,
        quality_evaluator: ArtifactQualityEvaluator | None = None,
        now=lambda: datetime.now(timezone.utc),
    ) -> None:
        self.foundation = foundation
        self.adapter = adapter
        self.case_id = case_id
        self.session_id = session_id
        self.sources = sources
        self.source_set_hash = adapter.source_set_hash(tuple(item.source for item in sources))
        self.analyzer_contract = analyzer_contract
        self.quality_evaluator = quality_evaluator
        self.now = now
        self._context_lock = asyncio.Lock()

    def _command_base(
        self,
        command_type: str,
        operation_key: str,
        *,
        expected_revision: int | None = None,
        actor: UUID | Literal["SYSTEM"] = "SYSTEM",
        correlation_id: UUID | None = None,
    ) -> dict[str, Any]:
        command_id = _stable_id(self.case_id, command_type, operation_key)
        values: dict[str, Any] = {
            "protocol_version": c.PROTOCOL_VERSION,
            "command_id": command_id,
            "case_id": self.case_id,
            "session_id": self.session_id,
            "correlation_id": correlation_id or _stable_id(command_id, "correlation"),
            "causation_id": None,
            "idempotency_key": f"v4-{command_type.lower()}-{operation_key}",
            "issued_at": self.now(),
            "acting_actor_id": actor,
        }
        if expected_revision is not None:
            values["expected_case_revision"] = expected_revision
        return values

    def _provider_base(
        self,
        *,
        operation: c.AnalyzerOperation,
        operation_key: str,
        context: c.AnalyzerContextBinding,
        based_on_revision: int,
    ) -> dict[str, Any]:
        run_id = _stable_id(self.case_id, operation.value, operation_key)
        return {
            "protocol_version": c.PROTOCOL_VERSION,
            "request_type": operation,
            "client_request_id": f"specops-{operation.value.lower()}-{run_id}",
            "analyzer_run_id": run_id,
            "context_id": context.context_id,
            "provider_conversation_id": context.provider_conversation_id,
            "source_set_hash": self.source_set_hash,
            "analyzer_contract": self.analyzer_contract,
            "based_on_case_revision": based_on_revision,
        }

    async def ensure_context(self) -> c.AnalyzerContextBinding:
        async with self._context_lock:
            active = self.foundation.active_analyzer_context(self.case_id)
            if active is not None:
                mismatch = (
                    active.source_set.source_set_hash != self.source_set_hash
                    or active.analyzer_contract != self.analyzer_contract
                    or active.model != "gpt-5.6-terra"
                    or active.reasoning_effort is not c.ReasoningEffort.MEDIUM
                    or not active.conversation_state_persisted
                    or not active.response_store_enabled
                )
                available = False if mismatch else await self.adapter.context_is_available(active)
                if available:
                    return active
                reason = (
                    "SOURCE_SET_CHANGED"
                    if active.source_set.source_set_hash != self.source_set_hash
                    else "ANALYZER_CONTRACT_CHANGED"
                    if active.analyzer_contract != self.analyzer_contract
                    else "PROFILE_MISMATCH"
                    if mismatch
                    else "CONVERSATION_UNAVAILABLE"
                )
                revision = self.foundation.case_revision(self.case_id)
                values = self._command_base(
                    "INVALIDATE_ANALYZER_CONTEXT",
                    str(active.context_id),
                    expected_revision=revision,
                )
                values.update(
                    command_type="INVALIDATE_ANALYZER_CONTEXT",
                    context_id=active.context_id,
                    reason_code=reason,
                )
                self.foundation.execute(c.InvalidateAnalyzerContextCommand(**values))
                await self.adapter.release_context(active)

            prepared = await self.adapter.prepare_context(self.sources)
            revision = self.foundation.case_revision(self.case_id)
            context_id = _stable_id(
                self.case_id,
                "context",
                prepared.provider_conversation_id,
                self.source_set_hash,
            )
            placeholder = c.AnalyzerContextBinding(
                protocol_version=c.PROTOCOL_VERSION,
                context_id=context_id,
                session_id=self.session_id,
                provider=c.ProviderName.OPENAI,
                provider_conversation_id=prepared.provider_conversation_id,
                bootstrap_response_id="pending",
                model="gpt-5.6-terra",
                reasoning_effort=c.ReasoningEffort.MEDIUM,
                conversation_state_persisted=True,
                response_store_enabled=True,
                analyzer_contract=self.analyzer_contract,
                source_set=prepared.source_set,
                status=c.ContextStatus.ACTIVE,
                created_at=self.now(),
                invalidated_at=None,
                invalidation_reason=None,
            )
            request = _request(
                c.BootstrapAnalyzerRequest,
                dict(
                    self._provider_base(
                        operation=c.AnalyzerOperation.BOOTSTRAP,
                        operation_key=str(context_id),
                        context=placeholder,
                        # Activation is the immediately preceding Foundation commit.
                        based_on_revision=revision + 1,
                    ),
                    source_set=prepared.source_set,
                    requested_output="INTERVIEW_BRIEF_CANDIDATE",
                ),
            )
            try:
                bootstrapped = await self.adapter.bootstrap(
                    request,
                    prepared=prepared,
                    session_id=self.session_id,
                )
            except Exception:
                await self.adapter.release_prepared(prepared)
                raise
            activate_values = self._command_base(
                "ACTIVATE_ANALYZER_CONTEXT", str(context_id), expected_revision=revision
            )
            activate_values.update(
                command_type="ACTIVATE_ANALYZER_CONTEXT", context=bootstrapped.context
            )
            self.foundation.execute(c.ActivateAnalyzerContextCommand(**activate_values))
            admit_values = self._command_base(
                "ADMIT_INTERVIEW_BRIEF",
                str(request.analyzer_run_id),
                expected_revision=revision + 1,
            )
            admit_values.update(
                command_type="ADMIT_INTERVIEW_BRIEF",
                analyzer_run_id=request.analyzer_run_id,
                context_id=context_id,
                provider_request_hash=request.request_hash,
                candidate=bootstrapped.candidate,
            )
            self.foundation.execute(c.AdmitInterviewBriefCommand(**admit_values))
            return bootstrapped.context

    async def record_final_transcript(
        self, value: FinalTranscriptInput
    ) -> TranscriptAnalysisReceipt:
        """Commit a final Gemini or HTTP text turn through one command path."""

        idempotency = f"v4-record-final-transcript-{value.provider_request_id}"
        replay = self.foundation.idempotent_receipt(self.case_id, idempotency)
        if replay is not None:
            assert isinstance(replay, c.TranscriptRecordedReceipt)
            return TranscriptAnalysisReceipt(transcript=replay, analysis=None, duplicate=True)

        prior = self.foundation.latest_final_transcript(self.case_id)
        expected_sequence = 1 if prior is None else prior.sequence_number + 1
        if value.turn_sequence != expected_sequence:
            raise ValueError("final transcript sequence is stale or has a gap")
        revision = self.foundation.case_revision(self.case_id)
        event_id = _stable_id(self.case_id, "transcript", value.provider_request_id)
        correlation_id = _stable_id(event_id, "correlation")
        event = c.TranscriptFinalizedEvent(
            protocol_version=c.PROTOCOL_VERSION,
            event_type="TRANSCRIPT_FINALIZED",
            event_id=event_id,
            case_id=self.case_id,
            session_id=self.session_id,
            correlation_id=correlation_id,
            causation_id=None,
            event_sequence=value.turn_sequence,
            observed_case_revision=revision,
            occurred_at=self.now(),
            producer="VOICE",
            turn_id=_stable_id(event_id, "turn"),
            transcript_artifact_id=_stable_id(self.case_id, "transcript-artifact"),
            transcript_version=1,
            transcript_hash=transcript_hash(value.text),
            actor=c.TranscriptActor(value.actor),
            speaker_actor_id=value.speaker_actor_id,
            speaker_attribution_method=c.SpeakerAttributionMethod.VERBAL_SELF_ASSERTION,
            sequence_number=value.turn_sequence,
            text=value.text,
        )
        command_values = self._command_base(
            "RECORD_FINAL_TRANSCRIPT",
            value.provider_request_id,
            expected_revision=revision,
            correlation_id=correlation_id,
        )
        command_values["idempotency_key"] = idempotency
        command_values.update(command_type="RECORD_FINAL_TRANSCRIPT", transcript=event)
        transcript_receipt = self.foundation.execute(c.RecordFinalTranscriptCommand(**command_values))
        assert isinstance(transcript_receipt, c.TranscriptRecordedReceipt)

        context = await self.ensure_context()
        snapshot = self.foundation.semantic_snapshot(self.case_id)
        prior_binding = None
        if prior is not None:
            prior_binding = c.PriorTranscriptBinding(
                transcript_event_id=prior.event_id,
                transcript_hash=prior.transcript_hash,
                sequence_number=prior.sequence_number,
            )
        request = _request(
            c.AnalyzeFinalTurnRequest,
            dict(
                self._provider_base(
                    operation=c.AnalyzerOperation.TURN_ANALYSIS,
                    operation_key=value.provider_request_id,
                    context=context,
                    based_on_revision=snapshot.case_revision,
                ),
                transcript=c.FinalizedTranscriptInput(
                    transcript_event_id=event.event_id,
                    transcript_hash=event.transcript_hash,
                    speaker_actor_id=event.speaker_actor_id,
                    actor=event.actor,
                    sequence_number=event.sequence_number,
                    text=event.text,
                ),
                prior_transcript=prior_binding,
                foundation_snapshot=snapshot,
                requested_output="TURN_ANALYSIS_CANDIDATE",
            ),
        )
        candidate = await self.adapter.execute(request, context=context)
        assert isinstance(candidate, c.TurnAnalysisCandidate)
        admit_values = self._command_base(
            "ADMIT_TURN_ANALYSIS",
            str(request.analyzer_run_id),
            expected_revision=snapshot.case_revision,
        )
        admit_values.update(
            command_type="ADMIT_TURN_ANALYSIS",
            analyzer_run_id=request.analyzer_run_id,
            context_id=context.context_id,
            provider_request_hash=request.request_hash,
            candidate=candidate,
        )
        analysis = self.foundation.execute(c.AdmitTurnAnalysisCommand(**admit_values))
        assert isinstance(analysis, c.ProposalAdmissionReceipt)
        return TranscriptAnalysisReceipt(
            transcript=transcript_receipt,
            analysis=analysis,
            duplicate=False,
        )

    async def execute_operation(
        self, request: c.AnalyzerProviderRequest
    ) -> ProviderOperationAdmission:
        """Execute/admit one of the five post-bootstrap narrow operations."""

        if request.request_type is c.AnalyzerOperation.BOOTSTRAP:
            raise ValueError("bootstrap lifecycle is owned by ensure_context")
        context = await self.ensure_context()
        if request.context_id != context.context_id:
            raise ValueError("operation is not bound to the active context")
        if request.request_hash != analyzer_request_hash(request.model_dump(mode="json")):
            raise ValueError("operation request hash is invalid")
        candidate = await self.adapter.execute(request, context=context)
        common = dict(
            provider_request_hash=request.request_hash,
            candidate=candidate,
        )
        if isinstance(request, c.AnalyzeFinalTurnRequest):
            model = c.AdmitTurnAnalysisCommand
            command_type = "ADMIT_TURN_ANALYSIS"
            common.update(analyzer_run_id=request.analyzer_run_id, context_id=context.context_id)
        elif isinstance(request, c.ReplenishGuidanceRequest):
            model = c.AdmitGuidanceCommand
            command_type = "ADMIT_GUIDANCE"
            common.update(analyzer_run_id=request.analyzer_run_id, context_id=context.context_id)
        elif isinstance(request, c.GenerateReviewNarrationRequest):
            model = c.AdmitReviewNarrationCommand
            command_type = "ADMIT_REVIEW_NARRATION"
            common.update(analyzer_run_id=request.analyzer_run_id, context_id=context.context_id)
        elif isinstance(request, c.SpecPackageSynthesisRequest):
            model = c.AdmitSpecPackageSynthesisCommand
            command_type = "ADMIT_SPEC_PACKAGE_SYNTHESIS"
            common.update(target=request.target, identity_plan=request.identity_plan)
        elif isinstance(request, c.TechnicalContractSynthesisRequest):
            model = c.AdmitTechnicalContractSynthesisCommand
            command_type = "ADMIT_TECHNICAL_CONTRACT_SYNTHESIS"
            common.update(
                target=request.target,
                identity_plan=request.identity_plan,
                confirmed_spec=request.confirmed_spec,
            )
        else:  # pragma: no cover - closed generated union
            raise TypeError("unknown Workshop analyzer operation")
        values = self._command_base(
            command_type,
            str(request.analyzer_run_id),
            expected_revision=request.based_on_case_revision,
        )
        values.update(command_type=command_type, **common)
        receipt = self.foundation.execute(model(**values))
        return ProviderOperationAdmission(candidate=candidate, receipt=receipt)

    async def replenish_guidance(
        self, runway_state: c.RunwayStateSnapshot, *, operation_key: str
    ) -> ProviderOperationAdmission:
        context = await self.ensure_context()
        snapshot = self.foundation.semantic_snapshot(self.case_id)
        request = _request(
            c.ReplenishGuidanceRequest,
            dict(
                self._provider_base(
                    operation=c.AnalyzerOperation.GUIDANCE,
                    operation_key=operation_key,
                    context=context,
                    based_on_revision=snapshot.case_revision,
                ),
                foundation_snapshot=snapshot,
                runway_state=runway_state,
                requested_output="GUIDANCE_CANDIDATE",
            ),
        )
        return await self.execute_operation(request)

    async def audit_latest_artifact(
        self, artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"]
    ) -> q.ArtifactQualityAuditReceipt:
        """Evaluate one immutable draft in a fresh provider context and admit it."""

        if self.quality_evaluator is None:
            raise RuntimeError("the production artifact quality evaluator is not configured")
        record = self.foundation.latest_artifact_record(self.case_id, artifact_type)
        if record is None:
            raise ValueError("no synthesized artifact exists for quality evaluation")
        context = await self.ensure_context()
        audit_id = _stable_id(
            self.case_id,
            "artifact-quality-audit",
            record["artifact_id"],
            record["artifact_version"],
            record["payload_hash"],
        )
        evaluator_run_id = _stable_id(audit_id, "evaluator-run")
        bundle = build_audit_bundle(
            audit_id=audit_id,
            evaluator_run_id=evaluator_run_id,
            case_id=self.case_id,
            session_id=self.session_id,
            based_on_case_revision=self.foundation.case_revision(self.case_id),
            artifact_record=record,
            sources=self.sources,
            transcripts=self.foundation.final_transcripts(self.case_id),
            semantic_snapshot=self.foundation.semantic_snapshot(self.case_id),
            semantic_quality_contract_hash=(
                self.analyzer_contract.semantic_quality_contract_hash
            ),
            confirmed_spec=(
                self.foundation.confirmed_spec_binding(self.case_id)
                if artifact_type == "TECHNICAL_CONTRACT"
                else None
            ),
        )
        prepared_receipt = self.foundation.prepare_artifact_quality_audit(
            q.PrepareArtifactQualityAuditCommand(
                protocol_version=q.PROTOCOL_VERSION,
                command_id=_stable_id(audit_id, "prepare"),
                idempotency_key=f"artifact-quality-prepare-{audit_id}",
                expected_case_revision=bundle.based_on_case_revision,
                bundle=bundle,
            )
        )
        if prepared_receipt.existing_receipt is not None:
            return prepared_receipt.existing_receipt
        provider_context = self.foundation.artifact_quality_provider_context(audit_id)
        if provider_context is None:
            prepared = await self.quality_evaluator.prepare(
                bundle,
                prohibited_conversation_id=context.provider_conversation_id,
            )
            self.foundation.bind_artifact_quality_evaluator(
                q.BindArtifactQualityEvaluatorCommand(
                    protocol_version=q.PROTOCOL_VERSION,
                    command_id=_stable_id(audit_id, "bind-provider-context"),
                    audit_id=audit_id,
                    request_hash=bundle.request_hash,
                    provider=prepared.provider,
                    model=prepared.model,
                    reasoning_effort="medium",
                    provider_conversation_id=prepared.provider_conversation_id,
                    client_request_id=prepared.client_request_id,
                    started_at=prepared.started_at,
                )
            )
        else:
            started_at = datetime.fromisoformat(
                provider_context["started_at"].replace("Z", "+00:00")
            )
            prepared = PreparedArtifactQualityContext(
                provider=provider_context["provider"],
                model=provider_context["model"],
                reasoning_effort=provider_context["reasoning_effort"],
                provider_conversation_id=provider_context["provider_conversation_id"],
                client_request_id=provider_context["client_request_id"],
                started_at=started_at,
            )
        evaluation = await self.quality_evaluator.evaluate(bundle, prepared=prepared)
        receipt = self.foundation.admit_artifact_quality_audit(
            q.AdmitArtifactQualityAuditCommand(
                protocol_version=q.PROTOCOL_VERSION,
                command_id=_stable_id(audit_id, "admit"),
                idempotency_key=f"artifact-quality-admit-{audit_id}",
                expected_case_revision=bundle.based_on_case_revision,
                bundle=bundle,
                execution=evaluation.execution,
                candidate=evaluation.candidate,
            )
        )
        await self.quality_evaluator.release(prepared)
        return receipt

    def materialize_latest_artifact_review(
        self,
        artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"],
        *,
        operation_key: str,
    ) -> c.ArtifactReviewReceipt:
        record = self.foundation.latest_artifact_record(self.case_id, artifact_type)
        if record is None:
            raise ValueError("no synthesized artifact exists for review")
        revision = self.foundation.case_revision(self.case_id)
        values = self._command_base(
            "MATERIALIZE_ARTIFACT_REVIEW",
            operation_key,
            expected_revision=revision,
        )
        values.update(
            command_type="MATERIALIZE_ARTIFACT_REVIEW",
            subject=c.ArtifactReviewSubjectBinding(
                artifact_type=artifact_type,
                artifact_id=UUID(record["artifact_id"]),
                artifact_key=record["artifact_key"],
                artifact_version=record["artifact_version"],
                record_revision=record["record_revision"],
                payload_hash=record["payload_hash"],
            ),
            view_mode="REVIEW",
        )
        receipt = self.foundation.execute(c.MaterializeArtifactReviewCommand(**values))
        assert isinstance(receipt, c.ArtifactReviewReceipt)
        return receipt

    def confirm_current_artifact(
        self,
        *,
        actor_authentication: c.ActorAuthentication,
        confirmation_transcript_event_id: UUID,
    ) -> c.ArtifactConfirmationReceipt:
        projection = self.foundation.current_artifact_review(self.case_id)
        if projection is None or projection["confirmed"]:
            raise ValueError("no current unconfirmed artifact review exists")
        source = projection["view"]["source"]
        artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"] = (
            "SPEC_PACKAGE"
            if projection["view"]["view_type"] == "spec_package_view"
            else "TECHNICAL_CONTRACT"
        )
        revision = self.foundation.case_revision(self.case_id)
        values = self._command_base(
            "CONFIRM_ARTIFACT",
            str(projection["confirmation_id"]),
            expected_revision=revision,
            actor=actor_authentication.actor_id,
        )
        values.update(
            command_type="CONFIRM_ARTIFACT",
            actor_authentication=actor_authentication,
            binding=c.ArtifactConfirmationBinding(
                artifact_type=artifact_type,
                artifact_id=UUID(source["artifact_id"]),
                artifact_key=source["artifact_key"],
                artifact_version=source["artifact_version"],
                record_revision=source["record_revision"],
                payload_hash=source["payload_hash"],
                confirmation_id=projection["confirmation_id"],
                view_id=projection["view_id"],
                view_hash=projection["view_hash"],
            ),
            confirmation_transcript_event_id=confirmation_transcript_event_id,
            approved_exception_ids=(),
        )
        receipt = self.foundation.execute(c.ConfirmArtifactCommand(**values))
        assert isinstance(receipt, c.ArtifactConfirmationReceipt)
        return receipt

    async def narrate_current_review(self, *, operation_key: str) -> ProviderOperationAdmission:
        context = await self.ensure_context()
        view = self.foundation.current_decision_view(self.case_id)
        if view is None:
            raise ValueError("no current Foundation decision review exists")
        request = _request(
            c.GenerateReviewNarrationRequest,
            dict(
                self._provider_base(
                    operation=c.AnalyzerOperation.REVIEW_NARRATION,
                    operation_key=operation_key,
                    context=context,
                    based_on_revision=view.based_on_case_revision,
                ),
                review_view=view,
                requested_output="REVIEW_NARRATION_CANDIDATE",
            ),
        )
        return await self.execute_operation(request)

    async def synthesize_artifact(
        self,
        artifact_type: Literal["SPEC_PACKAGE", "TECHNICAL_CONTRACT"],
        *,
        operation_key: str,
    ) -> ProviderOperationAdmission:
        context = await self.ensure_context()
        entity_kinds = self.foundation.artifact_identity_allocation_policy(artifact_type)
        plan = self.foundation.issue_artifact_identity_plan(
            self.case_id, artifact_type, entity_kinds
        )
        snapshot = self.foundation.semantic_snapshot(self.case_id)
        semantic_json = canonical_bytes(snapshot).decode("utf-8")
        if domain_hash(
            "SPECOPS:SEMANTIC_STATE:v1", snapshot.model_dump(mode="json")
        ) != plan.semantic_state_hash:
            raise ValueError("Foundation identity plan does not bind the canonical semantic state")
        common = dict(
            self._provider_base(
                operation=(
                    c.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS
                    if artifact_type == "SPEC_PACKAGE"
                    else c.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS
                ),
                operation_key=operation_key,
                context=context,
                based_on_revision=snapshot.case_revision,
            ),
            target=plan.target,
            identity_plan=plan,
            foundation_snapshot=snapshot,
            canonical_semantic_state_json=semantic_json,
        )
        if artifact_type == "SPEC_PACKAGE":
            request = _request(
                c.SpecPackageSynthesisRequest,
                dict(
                    common,
                    payload_schema_id="spec-package-payload",
                    payload_schema_version="4.0.0",
                    requested_output="SPEC_PACKAGE_SYNTHESIS_CANDIDATE",
                ),
            )
        else:
            confirmed = self.foundation.confirmed_spec_binding(self.case_id)
            if confirmed is None:
                raise ValueError("Technical Contract synthesis requires an exact confirmed Spec")
            request = _request(
                c.TechnicalContractSynthesisRequest,
                dict(
                    common,
                    confirmed_spec=confirmed,
                    payload_schema_id="technical-contract-payload",
                    payload_schema_version="4.0.0",
                    requested_output="TECHNICAL_CONTRACT_SYNTHESIS_CANDIDATE",
                ),
            )
        admission = await self.execute_operation(request)
        if self.quality_evaluator is None:
            return admission
        audit = await self.audit_latest_artifact(artifact_type)
        return ProviderOperationAdmission(
            candidate=admission.candidate,
            receipt=admission.receipt,
            quality_audit=audit,
        )

    def apply_voice_selection(
        self,
        selection: c.VoiceConfirmationSelectionCandidate,
        authentication: c.ActorAuthentication,
    ) -> c.DecisionBatchResponseReceipt:
        """Map Gemini's mechanical selection to exact Foundation review items."""

        view = self.foundation.current_decision_view(self.case_id)
        if view is None or (
            selection.decision_batch_view_id != view.view_id
            or selection.decision_batch_view_hash != view.view_hash
        ):
            raise ValueError("voice selection is not bound to the current review")
        by_handle = {item.handle: item for item in view.items}
        actions = []
        for chosen in selection.selections:
            item = by_handle.get(chosen.handle)
            if item is None:
                raise ValueError("voice selection contains an unknown handle")
            actions.append(
                c.DecisionBatchItemActionCommand(
                    review_item_id=item.review_item_id,
                    handle=item.handle,
                    pending_decision_id=item.pending_decision_id,
                    expected_pending_decision_version=item.pending_decision_version,
                    action=chosen.action,
                    revision_span=chosen.revision_span,
                )
            )
        values = self._command_base(
            "APPLY_DECISION_BATCH_RESPONSE",
            str(selection.selection_event_id),
            actor=selection.speaker_actor_id,
            correlation_id=_stable_id(selection.selection_event_id, "correlation"),
        )
        values.update(
            command_type="APPLY_DECISION_BATCH_RESPONSE",
            observed_case_revision=selection.observed_case_revision,
            actor_authentication=authentication,
            selection=selection,
            decision_batch_view_id=view.view_id,
            decision_batch_view_hash=view.view_hash,
            response_transcript_event_id=selection.transcript_event_id,
            item_actions=tuple(actions),
            unmentioned_item_policy="REMAIN_PENDING",
        )
        receipt = self.foundation.execute(c.ApplyDecisionBatchResponseCommand(**values))
        assert isinstance(receipt, c.DecisionBatchResponseReceipt)
        return receipt

    def apply_review_selection(
        self,
        *,
        operation_key: str,
        response_transcript_event_id: UUID,
        selections: tuple[c.VoiceConfirmationSelectionItemCandidate, ...],
        authentication: c.ActorAuthentication,
    ) -> c.DecisionBatchResponseReceipt:
        """Bind a browser selection to the current Foundation view.

        The frozen V4 command contract names its mechanical selection object
        ``VoiceConfirmationSelectionCandidate``.  Browser controls reuse that
        exact Foundation command path; they supply only handles/actions while
        the server binds the current revision and immutable view identifiers.
        """

        view = self.foundation.current_decision_view(self.case_id)
        if view is None:
            raise ValueError("no current Foundation decision review exists")
        selection = c.VoiceConfirmationSelectionCandidate(
            protocol_version=c.PROTOCOL_VERSION,
            output_type="VOICE_CONFIRMATION_SELECTION_CANDIDATE",
            producer="VOICE",
            selection_event_id=_stable_id(
                self.case_id, "browser-review-selection", operation_key
            ),
            mapping_status=c.ConfirmationMappingStatus.MAPPED,
            decision_batch_view_id=view.view_id,
            decision_batch_view_hash=view.view_hash,
            observed_case_revision=self.foundation.case_revision(self.case_id),
            transcript_event_id=response_transcript_event_id,
            speaker_actor_id=authentication.actor_id,
            selections=selections,
            unmentioned_item_policy="REMAIN_PENDING",
            clarification_question=None,
        )
        return self.apply_voice_selection(selection, authentication)
