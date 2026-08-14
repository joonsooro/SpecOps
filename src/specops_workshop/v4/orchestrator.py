"""Deterministic production seam between Voice/HTTP, OpenAI, and Foundation.

Only this module is allowed to call the stored-Conversation adapter.  Provider
outputs are always proposals and are admitted through the Workshop Protocol
Foundation commands before becoming observable application state.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from specops_contracts import artifact_quality_v1 as q
from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import analyzer_request_hash, canonical_bytes, domain_hash, transcript_hash
from specops_workflow.workshop_protocol import (
    FoundationProtocolError,
    WorkshopFoundationService,
)
from specops_workflow.workshop_completion import (
    CompletionUtterance,
    classify_completion_utterance,
)

from .openai_adapter import (
    BootstrapResult,
    PreparedProviderContext,
    ProviderAdapterError,
    ProviderCleanupReceipt,
    ProviderSourceUpload,
    StoredConversationOpenAIAdapter,
)
from .artifact_quality import build_audit_bundle
from .artifact_quality_adapter import (
    ArtifactQualityEvaluator,
    PreparedArtifactQualityContext,
)


PRODUCTION_NAMESPACE = UUID("e52d201a-e1d0-4df7-90e5-39ac4091998d")
ZERO_HASH = "sha256:" + "0" * 64
RESTART_GRACE_SECONDS = 15 * 60
CLEANUP_RETRY_SECONDS = 30
# Keep the established persisted label for restart compatibility; the single
# slot now covers either of the two allowlisted BOOTSTRAP correction causes.
BOOTSTRAP_CORRECTION_OPERATION = "BOOTSTRAP_GROUNDING_CORRECTION"


def _stable_id(*parts: object) -> UUID:
    return uuid5(PRODUCTION_NAMESPACE, ":".join(str(part) for part in parts))


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _parse_instant(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


class WorkshopCompletionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    receipt: c.WorkshopCompletionReceipt
    state: Literal["FINISHING_ANALYSIS", "CLEANUP_PENDING", "COMPLETE"]
    replayed: bool


class CompletionTurnOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    action: Literal["NONE", "CONFIRMATION_REQUIRED", "COMPLETE"]
    completion: WorkshopCompletionOutcome | None = None


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

    def _bootstrap_has_unbound_exact_quote(
        self, candidate: c.InterviewBriefCandidate
    ) -> bool:
        """Content-free preflight for the one correctable grounding failure."""

        source_text_by_role: dict[c.SourceRole, str] = {}
        for item in self.sources:
            source_text_by_role[item.source.role] = item.content.decode("utf-8")
        for evidence in candidate.evidence_candidates:
            locator = evidence.locator
            if not isinstance(locator, c.QuoteSearchLocator):
                continue
            source_text = source_text_by_role.get(evidence.source_role)
            if source_text is None or evidence.quoted_text_candidate != locator.exact_quote:
                return True
            occurrence_count = 0
            start = 0
            while True:
                found = source_text.find(locator.exact_quote, start)
                if found < 0:
                    break
                occurrence_count += 1
                if occurrence_count >= locator.occurrence:
                    break
                start = found + 1
            if occurrence_count < locator.occurrence:
                return True
        return False

    def _terminalize_known_preparation_failure(self, failure_code: str) -> None:
        self.foundation.set_preparation_phase(
            self.case_id,
            "FAILED",
            failure_code=failure_code,
            cleanup_state="PENDING",
        )
        self.foundation.set_provider_resource_lifecycle(
            self.case_id,
            cleanup_state="PENDING",
            cleanup_reason="UNRECOVERABLE_PREPARATION_FAILURE",
            cleanup_available_at=self.now()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            cleanup_last_error_code=None,
        )

    async def _correct_bootstrap(
        self,
        *,
        prepared: PreparedProviderContext,
        placeholder: c.AnalyzerContextBinding,
        revision: int,
        correction_code: Literal[
            "UNBOUND_EXACT_QUOTE", "UNKNOWN_QUESTION_PROBLEM_REFERENCE"
        ],
    ) -> tuple[c.BootstrapAnalyzerRequest, BootstrapResult] | None:
        """Run the sole durable child request after one known completed failure."""

        start_method_name = (
            "start_bootstrap_grounding_correction"
            if correction_code == "UNBOUND_EXACT_QUOTE"
            else "start_bootstrap_graph_correction"
        )
        if not hasattr(self.adapter, start_method_name) or not hasattr(
            self.adapter, "finish_bootstrap"
        ):
            return None
        request = self._bootstrap_correction_request(
            prepared=prepared,
            placeholder=placeholder,
            revision=revision,
        )
        tracked = next(
            (
                item
                for item in self.foundation.pending_provider_responses(self.case_id)
                if item["client_request_id"] == request.client_request_id
                and item["operation"] == BOOTSTRAP_CORRECTION_OPERATION
            ),
            None,
        )
        response_id = None if tracked is None else tracked["provider_response_id"]
        if response_id is None:
            try:
                start_method = getattr(self.adapter, start_method_name)
                response_id = await start_method(request, prepared=prepared)
            except asyncio.CancelledError:
                # No Response ID came back. X-Client-Request-Id is correlation,
                # not an exactly-once key, so never create another correction.
                self.foundation.set_preparation_phase(
                    self.case_id,
                    "FAILED",
                    failure_code="BOOTSTRAP_CORRECTION_RESPONSE_ID_UNCERTAIN",
                    cleanup_state="RETAIN_UNCERTAIN",
                )
                raise
            except Exception:
                self.foundation.set_preparation_phase(
                    self.case_id,
                    "FAILED",
                    failure_code="BOOTSTRAP_CORRECTION_RESPONSE_ID_UNCERTAIN",
                    cleanup_state="RETAIN_UNCERTAIN",
                )
                raise
            self.foundation.checkpoint_provider_response(
                self.case_id,
                client_request_id=request.client_request_id,
                operation=BOOTSTRAP_CORRECTION_OPERATION,
                provider_response_id=response_id,
            )
        try:
            result = await self.adapter.finish_bootstrap(
                request,
                prepared=prepared,
                session_id=self.session_id,
                response_id=response_id,
            )
        except ProviderAdapterError as exc:
            if exc.receipt.code is not c.ProviderFailureCode.OUTPUT_INVALID:
                # The correction Response identity is durable. A later worker may
                # safely inspect that same Response; it must not create another.
                raise
            self._terminalize_known_preparation_failure(
                "BOOTSTRAP_CORRECTION_OUTPUT_INVALID"
            )
            return None
        return request, result

    def _bootstrap_correction_request(
        self,
        *,
        prepared: PreparedProviderContext,
        placeholder: c.AnalyzerContextBinding,
        revision: int,
    ) -> c.BootstrapAnalyzerRequest:
        return _request(
            c.BootstrapAnalyzerRequest,
            dict(
                self._provider_base(
                    operation=c.AnalyzerOperation.BOOTSTRAP,
                    # Preserve the deployed child identity across restarts and
                    # upgrades; this legacy label now names the shared slot.
                    operation_key=f"{placeholder.context_id}:grounding-correction:1",
                    context=placeholder,
                    based_on_revision=revision + 1,
                ),
                source_set=prepared.source_set,
                requested_output="INTERVIEW_BRIEF_CANDIDATE",
            ),
        )

    async def ensure_context(self) -> c.AnalyzerContextBinding:
        # Narrow unit-test/application ports that predate Task 27 may provide a
        # fully admitted active context without the operational projection.
        if not hasattr(self.foundation, "preparation_projection"):
            active = self.foundation.active_analyzer_context(self.case_id)
            if active is None:
                raise RuntimeError("Workshop Analyzer context is not prepared")
            return active
        await self.prepare_workshop()
        active = self.foundation.active_analyzer_context(self.case_id)
        if active is None:
            raise RuntimeError("Workshop Analyzer context is not prepared")
        return active

    async def prepare_workshop(self) -> dict[str, Any]:
        """Resume mandatory preparation from the last durable provider checkpoint."""

        async with self._context_lock:
            projection = self.foundation.preparation_projection(self.case_id)
            if projection["phase"] in {"READY", "FAILED"}:
                return projection
            active = self.foundation.active_analyzer_context(self.case_id)
            runway = self.foundation.runway_projection(self.case_id)
            if active is not None and runway["depth"] == c.INITIAL_RUNWAY_DEPTH:
                self.foundation.set_preparation_phase(self.case_id, "READY")
                return self.foundation.preparation_projection(self.case_id)
            if active is not None and self.foundation.current_admitted_guidance(self.case_id) is None:
                self.foundation.set_preparation_phase(
                    self.case_id, "FAILED", failure_code="INSUFFICIENT_SAFE_RUNWAY"
                )
                return self.foundation.preparation_projection(self.case_id)

            self.foundation.set_preparation_phase(self.case_id, "VALIDATING_DOCUMENTS")
            if tuple(item.source.role for item in self.sources) != (
                c.SourceRole.PM_SPEC,
                c.SourceRole.TECHNICAL_CONTRACT,
            ):
                self.foundation.set_preparation_phase(
                    self.case_id, "FAILED", failure_code="SOURCE_ORDER_INVALID"
                )
                return self.foundation.preparation_projection(self.case_id)
            resources = self.foundation.preparation_resources(self.case_id)
            self.foundation.set_preparation_phase(self.case_id, "PREPARING_ANALYZER")
            prepared = None
            if all(
                hasattr(self.adapter, name)
                for name in ("upload_source", "create_conversation", "prepared_from_ids")
            ):
                file_ids = [resources["pm_file_id"], resources["technical_file_id"]]
                for index, item in enumerate(self.sources):
                    if file_ids[index] is None:
                        file_ids[index] = await self.adapter.upload_source(item)
                        self.foundation.checkpoint_preparation_resource(
                            self.case_id,
                            **{
                                "pm_file_id" if index == 0 else "technical_file_id": file_ids[index]
                            },
                        )
                conversation_id = resources["provider_conversation_id"]
                if conversation_id is None:
                    conversation_id = await self.adapter.create_conversation(self.sources)
                    self.foundation.checkpoint_preparation_resource(
                        self.case_id, provider_conversation_id=conversation_id
                    )
                prepared = self.adapter.prepared_from_ids(
                    self.sources, tuple(file_ids), conversation_id
                )
            else:
                prepared = await self.adapter.prepare_context(self.sources)
                self.foundation.checkpoint_preparation_resource(
                    self.case_id,
                    pm_file_id=prepared.source_set.ordered_sources[0].provider_file_id,
                    technical_file_id=prepared.source_set.ordered_sources[1].provider_file_id,
                    provider_conversation_id=prepared.provider_conversation_id,
                )
            resources = self.foundation.preparation_resources(self.case_id)
            self.foundation.set_preparation_phase(
                self.case_id, "ANALYZER_REVIEWING_DOCUMENTS"
            )
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
            admission_request = request
            correction_used = False
            if resources["bootstrap_candidate_json"] and resources["context_json"]:
                candidate = c.InterviewBriefCandidate.model_validate_json(
                    resources["bootstrap_candidate_json"]
                )
                context = c.AnalyzerContextBinding.model_validate_json(resources["context_json"])
                correction_used = any(
                    item["operation"] == BOOTSTRAP_CORRECTION_OPERATION
                    and item["provider_response_id"] == context.bootstrap_response_id
                    for item in self.foundation.pending_provider_responses(self.case_id)
                )
                if correction_used:
                    admission_request = self._bootstrap_correction_request(
                        prepared=prepared,
                        placeholder=placeholder,
                        revision=revision,
                    )
            else:
                try:
                    if all(
                        hasattr(self.adapter, name)
                        for name in ("start_bootstrap", "finish_bootstrap")
                    ):
                        response_id = resources["bootstrap_response_id"]
                        if response_id is None:
                            try:
                                response_id = await self.adapter.start_bootstrap(
                                    request, prepared=prepared
                                )
                            except asyncio.CancelledError:
                                # Without a returned Response ID, X-Client-Request-Id
                                # is correlation rather than an exactly-once key. Fail
                                # closed instead of blindly creating a second Response.
                                self.foundation.set_preparation_phase(
                                    self.case_id,
                                    "FAILED",
                                    failure_code="BOOTSTRAP_RESPONSE_ID_UNCERTAIN",
                                    cleanup_state="RETAIN_UNCERTAIN",
                                )
                                raise
                            except Exception:
                                # No provider identity came back. Correlation is
                                # insufficient proof that creating again is safe.
                                self.foundation.set_preparation_phase(
                                    self.case_id,
                                    "FAILED",
                                    failure_code="BOOTSTRAP_RESPONSE_ID_UNCERTAIN",
                                    cleanup_state="RETAIN_UNCERTAIN",
                                )
                                raise
                            self.foundation.checkpoint_preparation_resource(
                                self.case_id, bootstrap_response_id=response_id
                            )
                        bootstrapped = await self.adapter.finish_bootstrap(
                            request,
                            prepared=prepared,
                            session_id=self.session_id,
                            response_id=response_id,
                        )
                    else:
                        bootstrapped = await self.adapter.bootstrap(
                            request, prepared=prepared, session_id=self.session_id
                        )
                except ProviderAdapterError as exc:
                    if self.foundation.preparation_projection(self.case_id)[
                        "cleanup_state"
                    ] == "RETAIN_UNCERTAIN":
                        raise
                    if exc.receipt.code is not c.ProviderFailureCode.OUTPUT_INVALID:
                        # A known Response ID remains resumable; an unknown ID was
                        # already marked RETAIN_UNCERTAIN by the create path above.
                        raise
                    if (
                        exc.bootstrap_correction_code
                        != "UNKNOWN_QUESTION_PROBLEM_REFERENCE"
                    ):
                        self._terminalize_known_preparation_failure(
                            "BOOTSTRAP_OUTPUT_INVALID"
                        )
                        return self.foundation.preparation_projection(self.case_id)
                    corrected = await self._correct_bootstrap(
                        prepared=prepared,
                        placeholder=placeholder,
                        revision=revision,
                        correction_code="UNKNOWN_QUESTION_PROBLEM_REFERENCE",
                    )
                    if corrected is None:
                        if self.foundation.preparation_projection(self.case_id)[
                            "phase"
                        ] != "FAILED":
                            self._terminalize_known_preparation_failure(
                                "BOOTSTRAP_OUTPUT_INVALID"
                            )
                        return self.foundation.preparation_projection(self.case_id)
                    admission_request, bootstrapped = corrected
                    correction_used = True
                candidate = bootstrapped.candidate
                context = bootstrapped.context
                checkpoint = {
                    "bootstrap_candidate_json": candidate.model_dump_json(),
                    "context_json": context.model_dump_json(),
                }
                if not correction_used:
                    checkpoint[
                        "bootstrap_response_id"
                    ] = context.bootstrap_response_id
                self.foundation.checkpoint_preparation_resource(
                    self.case_id, **checkpoint
                )
            if (
                self._bootstrap_has_unbound_exact_quote(candidate)
                and not correction_used
            ):
                corrected = await self._correct_bootstrap(
                    prepared=prepared,
                    placeholder=placeholder,
                    revision=revision,
                    correction_code="UNBOUND_EXACT_QUOTE",
                )
                if corrected is not None:
                    admission_request, corrected_result = corrected
                    candidate = corrected_result.candidate
                    context = corrected_result.context
                    correction_used = True
                    # Keep the original BOOTSTRAP Response in its dedicated
                    # checkpoint; the correction Response is tracked separately.
                    self.foundation.checkpoint_preparation_resource(
                        self.case_id,
                        bootstrap_candidate_json=candidate.model_dump_json(),
                        context_json=context.model_dump_json(),
                    )
                elif self.foundation.preparation_projection(self.case_id)[
                    "phase"
                ] == "FAILED":
                    return self.foundation.preparation_projection(self.case_id)
            self.foundation.set_preparation_phase(
                self.case_id, "FORMULATING_WORKSHOP_PLAN"
            )
            if self.foundation.active_analyzer_context(self.case_id) is None:
                activate_values = self._command_base(
                    "ACTIVATE_ANALYZER_CONTEXT", str(context_id), expected_revision=revision
                )
                activate_values.update(
                    command_type="ACTIVATE_ANALYZER_CONTEXT", context=context
                )
                self.foundation.execute(c.ActivateAnalyzerContextCommand(**activate_values))
            admit_values = self._command_base(
                "ADMIT_INTERVIEW_BRIEF",
                str(admission_request.analyzer_run_id),
                expected_revision=self.foundation.case_revision(self.case_id),
            )
            admit_values.update(
                command_type="ADMIT_INTERVIEW_BRIEF",
                analyzer_run_id=admission_request.analyzer_run_id,
                context_id=context_id,
                provider_request_hash=admission_request.request_hash,
                candidate=candidate,
            )
            if self.foundation.current_admitted_guidance(self.case_id) is None:
                try:
                    self.foundation.execute(c.AdmitInterviewBriefCommand(**admit_values))
                except FoundationProtocolError as exc:
                    active = self.foundation.active_analyzer_context(self.case_id)
                    if active is not None:
                        invalidate_values = self._command_base(
                            "INVALIDATE_ANALYZER_CONTEXT",
                            f"preparation-rejected-{context_id}",
                            expected_revision=self.foundation.case_revision(self.case_id),
                        )
                        invalidate_values.update(
                            command_type="INVALIDATE_ANALYZER_CONTEXT",
                            context_id=context_id,
                            reason_code="PREPARATION_REJECTED",
                        )
                        self.foundation.execute(
                            c.InvalidateAnalyzerContextCommand(**invalidate_values)
                        )
                    failure_code = f"FOUNDATION_REJECTED_{exc.code.value}"
                    self.foundation.set_provider_resource_lifecycle(
                        self.case_id,
                        cleanup_state="PENDING",
                        cleanup_reason="UNRECOVERABLE_PREPARATION_FAILURE",
                        cleanup_available_at=self.now()
                        .astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        cleanup_last_error_code=None,
                    )
                    self.foundation.set_preparation_phase(
                        self.case_id,
                        "FAILED",
                        failure_code=failure_code,
                        cleanup_state="PENDING",
                    )
                    return self.foundation.preparation_projection(self.case_id)
            self.foundation.set_preparation_phase(
                self.case_id, "VALIDATING_INITIAL_RUNWAY"
            )
            if (
                self.foundation.runway_projection(self.case_id)["depth"]
                != c.INITIAL_RUNWAY_DEPTH
            ):
                self.foundation.set_preparation_phase(
                    self.case_id, "FAILED", failure_code="INSUFFICIENT_SAFE_RUNWAY"
                )
            else:
                self.foundation.set_preparation_phase(self.case_id, "READY")
            return self.foundation.preparation_projection(self.case_id)

    async def note_client_connected(self) -> str:
        """Cancel an unexpired last-client grace period or request safe rebuild."""

        async with self._context_lock:
            projection = self.foundation.preparation_projection(self.case_id)
            state = projection["cleanup_state"]
            if projection["workshop_complete_at"] is not None:
                return "WORKSHOP_COMPLETE"
            if state == "RESTART_GRACE":
                deadline = _parse_instant(projection["restart_grace_until"])
                if deadline is not None and self.now() < deadline:
                    self.foundation.set_provider_resource_lifecycle(
                        self.case_id,
                        cleanup_state="NOT_REQUIRED",
                        cleanup_reason=None,
                        last_client_disconnected_at=None,
                        restart_grace_until=None,
                        cleanup_available_at=None,
                        cleanup_last_error_code=None,
                    )
                    return "RESUMED_WITHIN_GRACE"
                return "REBUILD_AFTER_CLEANUP"
            if (
                state == "COMPLETED"
                and projection["cleanup_reason"] == "RESTART_GRACE_EXPIRED"
            ):
                self.foundation.reset_preparation_after_provider_cleanup(self.case_id)
                return "REBUILD_REQUIRED"
            if state in {"PENDING", "IN_PROGRESS", "RETRY_WAIT"}:
                return "REBUILD_AFTER_CLEANUP"
            if state == "RETAIN_UNCERTAIN":
                return "PROVIDER_OUTCOME_UNCERTAIN"
            return "ACTIVE"

    async def note_last_client_disconnected(
        self, *, disconnected_at: datetime | None = None
    ) -> str:
        """Start the 15-minute provider-retention grace at the observed disconnect."""

        observed_at = disconnected_at or self.now()
        async with self._context_lock:
            projection = self.foundation.preparation_projection(self.case_id)
            state = projection["cleanup_state"]
            if projection["workshop_complete_at"] is not None:
                return "WORKSHOP_COMPLETE"
            if state in {
                "RETAIN_UNCERTAIN",
                "PENDING",
                "IN_PROGRESS",
                "RETRY_WAIT",
                "COMPLETED",
            }:
                return state
            self.foundation.set_provider_resource_lifecycle(
                self.case_id,
                cleanup_state="RESTART_GRACE",
                cleanup_reason=None,
                last_client_disconnected_at=observed_at.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                restart_grace_until=(
                    observed_at + timedelta(seconds=RESTART_GRACE_SECONDS)
                )
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                cleanup_available_at=None,
                cleanup_last_error_code=None,
            )
            return "RESTART_GRACE"

    async def expire_restart_grace(self) -> bool:
        """Make stale tracked resources cleanup-eligible after jobs become terminal."""

        async with self._context_lock:
            projection = self.foundation.preparation_projection(self.case_id)
            if projection["cleanup_state"] != "RESTART_GRACE":
                return False
            deadline = _parse_instant(projection["restart_grace_until"])
            if deadline is None or self.now() < deadline:
                return False
            if any(
                job["state"] not in {"COMPLETED", "FAILED"}
                for job in self.foundation.analyzer_jobs(self.case_id)
            ):
                return False
            context = self.foundation.active_analyzer_context(self.case_id)
            if context is not None:
                values = self._command_base(
                    "INVALIDATE_ANALYZER_CONTEXT",
                    f"restart-grace-{context.context_id}",
                    expected_revision=self.foundation.case_revision(self.case_id),
                )
                values.update(
                    command_type="INVALIDATE_ANALYZER_CONTEXT",
                    context_id=context.context_id,
                    reason_code="WORKSHOP_CLOSED",
                )
                self.foundation.execute(c.InvalidateAnalyzerContextCommand(**values))
            self.foundation.set_preparation_phase(
                self.case_id,
                "FAILED",
                failure_code="RESTART_GRACE_EXPIRED",
                cleanup_state="PENDING",
            )
            self.foundation.set_provider_resource_lifecycle(
                self.case_id,
                cleanup_state="PENDING",
                cleanup_reason="RESTART_GRACE_EXPIRED",
                cleanup_available_at=self.now()
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                cleanup_last_error_code=None,
            )
            return True

    async def cleanup_preparation(self) -> dict[str, Any]:
        """Release only tracked resources whose provider outcome is confirmed."""

        async with self._context_lock:
            projection = self.foundation.preparation_projection(self.case_id)
            if projection["cleanup_state"] not in {
                "PENDING",
                "IN_PROGRESS",
                "RETRY_WAIT",
            }:
                return projection
            available_at = _parse_instant(projection["cleanup_available_at"])
            if (
                projection["cleanup_state"] == "RETRY_WAIT"
                and available_at is not None
                and self.now() < available_at
            ):
                return projection
            if self.foundation.active_analyzer_context(self.case_id) is not None:
                raise RuntimeError("active Analyzer context cannot be preparation-cleaned")
            if not hasattr(self.adapter, "release_resource_ids"):
                raise RuntimeError("provider adapter cannot resume preparation cleanup")
            self.foundation.set_provider_resource_lifecycle(
                self.case_id,
                cleanup_state="IN_PROGRESS",
                cleanup_available_at=None,
            )
            resources = self.foundation.preparation_resources(self.case_id)
            provider_responses = self.foundation.pending_provider_responses(
                self.case_id
            )
            try:
                receipt = await self.adapter.release_resource_ids(
                    resources["provider_conversation_id"],
                    tuple(
                        identity
                        for identity in (
                            resources["pm_file_id"],
                            resources["technical_file_id"],
                        )
                        if identity is not None
                    ),
                    response_id=resources["bootstrap_response_id"],
                    response_ids=tuple(
                        item["provider_response_id"] for item in provider_responses
                    ),
                )
            except asyncio.CancelledError:
                # IN_PROGRESS is deliberately resumable. The adapter receives
                # the same resource identities on the next worker lease.
                raise
            except Exception:
                self.foundation.set_provider_resource_lifecycle(
                    self.case_id,
                    cleanup_state="RETAIN_UNCERTAIN",
                    cleanup_available_at=None,
                    cleanup_last_error_code="DELETE_ADAPTER_EXCEPTION",
                )
                return self.foundation.preparation_projection(self.case_id)
            if not isinstance(receipt, ProviderCleanupReceipt):
                self.foundation.set_provider_resource_lifecycle(
                    self.case_id,
                    cleanup_state="RETAIN_UNCERTAIN",
                    cleanup_available_at=None,
                    cleanup_last_error_code="DELETE_RECEIPT_MISSING",
                )
                return self.foundation.preparation_projection(self.case_id)
            cleared: dict[str, None] = {}
            for deletion in receipt.deletions:
                if not deletion.confirmed_absent:
                    continue
                if (
                    deletion.resource_kind == "CONVERSATION"
                    and deletion.resource_id == resources["provider_conversation_id"]
                ):
                    cleared["provider_conversation_id"] = None
                if deletion.resource_kind == "FILE":
                    if deletion.resource_id == resources["pm_file_id"]:
                        cleared["pm_file_id"] = None
                    if deletion.resource_id == resources["technical_file_id"]:
                        cleared["technical_file_id"] = None
                if (
                    deletion.resource_kind == "RESPONSE"
                    and deletion.resource_id == resources["bootstrap_response_id"]
                ):
                    cleared["bootstrap_response_id"] = None
                if deletion.resource_kind == "RESPONSE" and any(
                    deletion.resource_id == item["provider_response_id"]
                    for item in provider_responses
                ):
                    self.foundation.clear_provider_response(
                        self.case_id, deletion.resource_id
                    )
            if cleared:
                self.foundation.checkpoint_preparation_resource(self.case_id, **cleared)
            remaining = self.foundation.preparation_resources(self.case_id)
            remaining_provider_responses = self.foundation.pending_provider_responses(
                self.case_id
            )
            remaining_ids = tuple(
                remaining[name]
                for name in (
                    "pm_file_id",
                    "technical_file_id",
                    "provider_conversation_id",
                    "bootstrap_response_id",
                )
                if remaining[name] is not None
            ) + tuple(
                item["provider_response_id"]
                for item in remaining_provider_responses
            )
            if not remaining_ids:
                self.foundation.set_provider_resource_lifecycle(
                    self.case_id,
                    cleanup_state="COMPLETED",
                    cleanup_available_at=None,
                    cleanup_last_error_code=None,
                )
            else:
                failed = tuple(
                    item for item in receipt.deletions if not item.confirmed_absent
                )
                safe_code = next(
                    (item.safe_error_code for item in failed if item.safe_error_code),
                    "DELETE_RECEIPT_INCOMPLETE",
                )
                if any(item.retryable for item in failed):
                    self.foundation.set_provider_resource_lifecycle(
                        self.case_id,
                        cleanup_state="RETRY_WAIT",
                        cleanup_available_at=(
                            self.now() + timedelta(seconds=CLEANUP_RETRY_SECONDS)
                        )
                        .astimezone(timezone.utc)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        cleanup_last_error_code=safe_code,
                    )
                else:
                    self.foundation.set_provider_resource_lifecycle(
                        self.case_id,
                        cleanup_state="RETAIN_UNCERTAIN",
                        cleanup_available_at=None,
                        cleanup_last_error_code=safe_code,
                    )
            return self.foundation.preparation_projection(self.case_id)

    async def complete_workshop(
        self,
        *,
        operation_key: str,
        source: c.WorkshopCompletionSource,
        completion_transcript_event_id: UUID | None = None,
        confirmation_transcript_event_id: UUID | None = None,
    ) -> WorkshopCompletionOutcome:
        """Persist one human completion claim and start terminal-work draining."""

        idempotency = f"v4-workshop-complete-{self.case_id}"
        async with self._context_lock:
            replay = self.foundation.workshop_completion_receipt(self.case_id)
            if replay is not None:
                assert isinstance(replay, c.WorkshopCompletionReceipt)
                self._advance_workshop_completion_locked()
                projection = self.foundation.workshop_completion_projection(self.case_id)
                return WorkshopCompletionOutcome(
                    receipt=replay,
                    state=projection["state"],
                    replayed=True,
                )

            actor = self.foundation.case_actor(self.case_id, "PM")
            values = self._command_base(
                "CLAIM_WORKSHOP_COMPLETE",
                operation_key,
                expected_revision=self.foundation.case_revision(self.case_id),
                actor=actor,
            )
            values["idempotency_key"] = idempotency
            values.update(
                command_type="CLAIM_WORKSHOP_COMPLETE",
                completion_source=source,
                completion_transcript_event_id=completion_transcript_event_id,
                confirmation_transcript_event_id=confirmation_transcript_event_id,
            )
            receipt = self.foundation.execute(c.ClaimWorkshopCompleteCommand(**values))
            assert isinstance(receipt, c.WorkshopCompletionReceipt)
            self._advance_workshop_completion_locked()
            projection = self.foundation.workshop_completion_projection(self.case_id)
            return WorkshopCompletionOutcome(
                receipt=receipt,
                state=projection["state"],
                replayed=False,
            )

    async def complete_from_final_transcript(
        self, transcript_event_id: UUID
    ) -> CompletionTurnOutcome:
        """Apply only explicit or immediately confirmed durable voice completion."""

        transcripts = self.foundation.final_transcripts(self.case_id)
        if not transcripts or transcripts[-1].event_id != transcript_event_id:
            raise ValueError("completion intent must bind the latest final transcript")
        current = transcripts[-1]
        classification = classify_completion_utterance(current.text)
        if classification is CompletionUtterance.EXPLICIT:
            completion = await self.complete_workshop(
                operation_key=f"voice-explicit-{current.event_id}",
                source=c.WorkshopCompletionSource.VOICE_EXPLICIT,
                completion_transcript_event_id=current.event_id,
            )
            return CompletionTurnOutcome(action="COMPLETE", completion=completion)
        if classification is CompletionUtterance.AMBIGUOUS:
            return CompletionTurnOutcome(action="CONFIRMATION_REQUIRED")
        if classification is CompletionUtterance.AFFIRMATIVE and len(transcripts) >= 2:
            intent = transcripts[-2]
            if classify_completion_utterance(intent.text) is CompletionUtterance.AMBIGUOUS:
                completion = await self.complete_workshop(
                    operation_key=f"voice-confirmed-{intent.event_id}-{current.event_id}",
                    source=c.WorkshopCompletionSource.VOICE_CONFIRMED,
                    completion_transcript_event_id=intent.event_id,
                    confirmation_transcript_event_id=current.event_id,
                )
                return CompletionTurnOutcome(action="COMPLETE", completion=completion)
        return CompletionTurnOutcome(action="NONE")

    async def advance_workshop_completion(self) -> bool:
        async with self._context_lock:
            return self._advance_workshop_completion_locked()

    def _advance_workshop_completion_locked(self) -> bool:
        projection = self.foundation.preparation_projection(self.case_id)
        if projection["workshop_complete_at"] is None:
            return False
        if any(
            job["state"] not in {"COMPLETED", "FAILED"}
            for job in self.foundation.analyzer_jobs(self.case_id)
        ):
            return False
        if projection["cleanup_state"] != "FINISHING_ANALYSIS":
            return False
        context = self.foundation.active_analyzer_context(self.case_id)
        if context is not None:
            values = self._command_base(
                "INVALIDATE_ANALYZER_CONTEXT",
                f"workshop-complete-{context.context_id}",
                expected_revision=self.foundation.case_revision(self.case_id),
            )
            values.update(
                command_type="INVALIDATE_ANALYZER_CONTEXT",
                context_id=context.context_id,
                reason_code="WORKSHOP_CLOSED",
            )
            self.foundation.execute(c.InvalidateAnalyzerContextCommand(**values))
        self.foundation.set_provider_resource_lifecycle(
            self.case_id,
            cleanup_state="PENDING",
            cleanup_reason="WORKSHOP_COMPLETE",
            last_client_disconnected_at=None,
            restart_grace_until=None,
            cleanup_available_at=self.now()
            .astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            cleanup_last_error_code=None,
        )
        return True

    async def record_final_transcript(
        self, value: FinalTranscriptInput
    ) -> TranscriptAnalysisReceipt:
        """Commit a final Gemini or HTTP text turn through one command path."""

        idempotency = f"v4-record-final-transcript-{value.provider_request_id}"
        replay = self.foundation.idempotent_receipt(self.case_id, idempotency)
        if replay is not None:
            assert isinstance(replay, c.TranscriptRecordedReceipt)
            analysis = None
            event_id = _stable_id(self.case_id, "transcript", value.provider_request_id)
            for job in self.foundation.analyzer_jobs(self.case_id):
                if job["subject_id"] == str(event_id) and job["admission_receipt_json"]:
                    parsed = c.ProposalAdmissionReceipt.model_validate_json(
                        job["admission_receipt_json"]
                    )
                    analysis = parsed
                    break
            return TranscriptAnalysisReceipt(transcript=replay, analysis=analysis, duplicate=True)

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

        return TranscriptAnalysisReceipt(
            transcript=transcript_receipt,
            analysis=None,
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
        candidate = await self._execute_provider(request, context=context)
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

    async def _execute_provider(
        self,
        request: c.AnalyzerProviderRequest,
        *,
        context: c.AnalyzerContextBinding,
    ) -> c.AnalyzerProviderCandidate:
        checkpointed_execute = getattr(
            self.adapter, "execute_with_response_checkpoint", None
        )
        if checkpointed_execute is None:
            return await self.adapter.execute(request, context=context)

        def checkpoint(response_id: str) -> None:
            self.foundation.checkpoint_provider_response(
                self.case_id,
                client_request_id=request.client_request_id,
                operation=c.AnalyzerOperation(request.request_type).value,
                provider_response_id=response_id,
            )

        return await checkpointed_execute(
            request,
            context=context,
            checkpoint=checkpoint,
        )

    async def _execute_turn_analysis_correction(
        self,
        request: c.AnalyzerProviderRequest,
        *,
        context: c.AnalyzerContextBinding,
        quarantined_candidate_keys: tuple[str, ...],
    ) -> c.AnalyzerProviderCandidate:
        """Run one isolated correction without opening another Analyzer operation."""

        if not isinstance(request, c.AnalyzeFinalTurnRequest):
            raise TypeError("grounding correction requires TURN_ANALYSIS")
        execute = getattr(
            self.adapter,
            "execute_turn_analysis_correction_with_response_checkpoint",
            None,
        )
        if execute is None:
            raise RuntimeError("Analyzer adapter does not support bounded turn correction")

        def checkpoint(response_id: str) -> None:
            self.foundation.checkpoint_provider_response(
                self.case_id,
                client_request_id=request.client_request_id,
                operation=c.AnalyzerOperation.TURN_ANALYSIS.value,
                provider_response_id=response_id,
            )

        return await execute(
            request,
            context=context,
            quarantined_candidate_keys=quarantined_candidate_keys,
            checkpoint=checkpoint,
        )

    async def _resume_provider_response(
        self,
        request: c.AnalyzerProviderRequest,
        *,
        context: c.AnalyzerContextBinding,
        response_id: str,
    ) -> c.AnalyzerProviderCandidate:
        resume = getattr(self.adapter, "resume_stored_response", None)
        if resume is None:
            raise RuntimeError("Analyzer adapter cannot resume a known Response")
        return await resume(
            request,
            context=context,
            response_id=response_id,
        )

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
        if self.foundation.runway_projection(self.case_id)["depth"] < 3:
            raise ValueError("active interview runway is endangered")
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

        view = self.foundation.decision_view_by_binding(
            self.case_id,
            selection.decision_batch_view_id,
            selection.decision_batch_view_hash,
        )
        if view is None:
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

        selection_event_id = _stable_id(
            self.case_id, "browser-review-selection", operation_key
        )
        response_idempotency_key = (
            f"v4-apply_decision_batch_response-{selection_event_id}"
        )
        replay_context = self.foundation.decision_response_replay_context(
            self.case_id, response_idempotency_key
        )
        view = (
            replay_context[0]
            if replay_context is not None
            else self.foundation.current_decision_view(self.case_id)
        )
        if view is None:
            raise ValueError("no current Foundation decision review exists")
        observed_case_revision = (
            replay_context[1]
            if replay_context is not None
            else self.foundation.case_revision(self.case_id)
        )
        selection = c.VoiceConfirmationSelectionCandidate(
            protocol_version=c.PROTOCOL_VERSION,
            output_type="VOICE_CONFIRMATION_SELECTION_CANDIDATE",
            producer="VOICE",
            selection_event_id=selection_event_id,
            mapping_status=c.ConfirmationMappingStatus.MAPPED,
            decision_batch_view_id=view.view_id,
            decision_batch_view_hash=view.view_hash,
            observed_case_revision=observed_case_revision,
            transcript_event_id=response_transcript_event_id,
            speaker_actor_id=authentication.actor_id,
            selections=selections,
            unmentioned_item_policy="REMAIN_PENDING",
            clarification_question=None,
        )
        return self.apply_voice_selection(selection, authentication)
