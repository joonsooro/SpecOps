from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid5

from specops_workflow.enums import AmbiguityCategory, ApprovalScope, Domain, Severity
from specops_workflow.canonical import sha256 as canonical_sha256
from specops_workflow.models import (
    AcceptanceCheck,
    ApproveSpecPackageItemCommand,
    CreateSpecPackageV2Command,
    MarkSpecPackageItemReadyCommand,
    QueryOne,
    RecordItemAmbiguityFindingCommand,
    Requirement,
    ReviseSpecPackageV2Command,
    SpecPackageItem,
    SpecPackagePayloadV2,
    TechnicalDecision,
)
from specops_workflow.models import LineRange

from .analyzer import (
    AcceptanceCheckProposal,
    AnalyzerProviderAvailabilityError,
    AnalyzerProviderSchemaError,
    AnalyzerTurnResult,
    CommittedPackageSemantics,
    CommittedSemanticContext,
    CompletePackageProposal,
    ControlIntent,
    ControlTarget,
    FindingProposal,
    GroundingChecker,
    GroundedSemanticText,
    ProposalDisposition,
    RequirementProposal,
    SelectedSemanticEvidence,
    SemanticAnalyzerRequest,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SpecAnalyzerProvider,
    SpecPackageItemProposal,
    TechnicalDecisionProposal,
    proposal_id,
    validate_phase,
)
from .analyzer_runtime import (
    AnalyzerDeadlineExceeded,
    AnalyzerSessionDeadline,
    analyzer_request_fingerprint,
    retrieval_checkpoint_fingerprint,
    source_snapshot_fingerprint,
)
from .contracts import (
    AnalyzerCheckpoint,
    AnalyzerCheckpointStage,
    AnalyzerCheckpointStatus,
    AnalyzerFailureKind,
    AnalyzerRecoveryAction,
    AnalyzerRecoveryView,
    PackageProposalRecord,
    ProposalStatus,
)
from .evidence import (
    AliasMaterializer,
    DeterministicEvidenceRetriever,
    EvidenceIndex,
    RegisteredMarkdownSnapshot,
    RetrievalOutcome,
    evidence_turn_fingerprint,
)
from .orchestration import WorkshopCoordinator
from .sessions import WorkshopStore
from .telemetry import SpanOutcome, TelemetryStage


GATE_NAMESPACE = UUID("aac80ba7-67e6-5538-a88b-3dac7ebde6ee")


class RegisteredEvidenceGrounding:
    """Fail-closed identity/range grounding; semantic fixtures may be injected."""
    def __init__(self, *, static_refs: dict[UUID, tuple[int, str, int | tuple[str, ...]]], store: WorkshopStore, session_id: UUID) -> None:
        self.static_refs = static_refs; self.store = store; self.session_id = session_id
        self.last_failure_code: str | None = None

    def supports(self, claim: str, evidence_refs: tuple) -> bool:
        self.last_failure_code = None
        if not claim.strip() or not evidence_refs:
            return self._reject("MISSING_CLAIM_OR_EVIDENCE")
        transcript = {
            snapshot.final_source_ref.model_dump_json(): snapshot.normalized_text
            for snapshot in self.store.latest_snapshots(self.session_id)
        }
        for ref in evidence_refs:
            transcript_text = transcript.get(ref.model_dump_json())
            if transcript_text is not None:
                if not self._semantic_overlap(claim, transcript_text):
                    return self._reject("TRANSCRIPT_SEMANTIC_MISMATCH")
                continue
            expected = self.static_refs.get(ref.artifact_id)
            if expected is None:
                return self._reject("UNKNOWN_SOURCE_ARTIFACT")
            line_source = None if expected is None else expected[2]
            line_count = line_source if isinstance(line_source, int) else len(line_source)
            if (
                expected is None
                or ref.version != expected[0]
                or ref.content_hash != expected[1]
                or not isinstance(ref.location, LineRange)
                or ref.location.end > line_count
            ):
                return self._reject("SOURCE_IDENTITY_OR_RANGE_MISMATCH")
            if isinstance(line_source, tuple):
                excerpt = "\n".join(line_source[ref.location.start - 1:ref.location.end])
                if not self._semantic_overlap(claim, excerpt):
                    return self._reject("SOURCE_SEMANTIC_MISMATCH")
        return True

    def _reject(self, code: str) -> bool:
        self.last_failure_code = code
        return False

    @staticmethod
    def _semantic_overlap(claim: str, evidence: str) -> bool:
        stop = {
            "a", "an", "and", "are", "as", "at", "be", "by", "does", "for", "from",
            "how", "in", "is", "it", "of", "on", "or", "should", "the", "this", "to",
            "what", "when", "which", "with",
        }
        def terms(value: str) -> set[str]:
            return {
                token for token in re.findall(r"[a-z0-9]+", value.casefold())
                if len(token) >= 3 and token not in stop
            }
        return bool(terms(claim).intersection(terms(evidence)))


class WorkshopGate:
    def __init__(
        self,
        store: WorkshopStore,
        foundation,
        analyzer: SpecAnalyzerProvider,
        grounding: GroundingChecker,
        *,
        clock,
        evidence_index: EvidenceIndex,
        evidence_snapshots: tuple[RegisteredMarkdownSnapshot, ...],
        business_context: str,
        dev_lead_actor_id: UUID,
        telemetry=None,
        default_effort: str = "medium",
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store = store; self.foundation = foundation; self.analyzer = analyzer; self.grounding = grounding; self.clock = clock
        self.evidence_index = evidence_index
        self.evidence_snapshots = evidence_snapshots
        self.business_context = business_context
        self.dev_lead_actor_id = dev_lead_actor_id
        self.telemetry = telemetry
        self.default_effort = default_effort
        self.monotonic = monotonic
        self.sleep = sleep
        self.coordinator = WorkshopCoordinator(
            store, foundation, clock=clock, telemetry=telemetry
        )

    async def analyze_final_turn(
        self,
        session_id: UUID,
        turn_sequence: int,
        *,
        effort: str | None = None,
        purpose: str = "TURN",
        edit_instruction: str | None = None,
    ) -> AnalyzerTurnResult:
        deadline = AnalyzerSessionDeadline.start(
            monotonic=self.monotonic,
            sleep=self.sleep,
        )
        resolved_effort = self.default_effort if effort is None else effort
        if resolved_effort != "medium":
            raise ValueError("Workshop analyzer reasoning effort is pinned to medium")
        session = self.store.get_session(session_id)
        snapshots = {
            value.turn_sequence: value
            for value in self.store.latest_snapshots(session_id)
        }
        final = snapshots.get(turn_sequence)
        if final is None:
            raise ValueError("only persisted provider-final PM evidence may be analyzed")

        committed_context = self._semantic_committed_context(session)
        source_fingerprint = source_snapshot_fingerprint(self.evidence_index)
        final_turn_fingerprint = evidence_turn_fingerprint(final.normalized_text)
        request_fingerprint = analyzer_request_fingerprint(
            source_fingerprint=source_fingerprint,
            final_turn_fingerprint=final_turn_fingerprint,
            committed_semantics_fingerprint=canonical_sha256(committed_context),
            edit_instruction_fingerprint=canonical_sha256(edit_instruction or ""),
            purpose=purpose,
            phase=session.conversation_phase.value,
        )
        request_id = uuid5(
            GATE_NAMESPACE,
            f"{session_id}:analyze:{turn_sequence}:{request_fingerprint}",
        )
        span_id = None
        if self.telemetry is not None:
            span_id = self.telemetry.start(
                session_id=session_id,
                stage=TelemetryStage.ANALYZER,
                operation_id=str(request_id),
            )

        checkpoint = self.store.analyzer_checkpoint(request_id)
        selected_aliases: tuple[str, ...] | None = None
        retrieval_fingerprint: str | None = None
        if checkpoint is not None:
            if (
                checkpoint.session_id != session_id
                or checkpoint.turn_sequence != turn_sequence
                or checkpoint.request_fingerprint != request_fingerprint
                or checkpoint.source_fingerprint != source_fingerprint
            ):
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=None,
                    failure_kind=AnalyzerFailureKind.GOVERNANCE,
                    recovery_action=AnalyzerRecoveryAction.LOCK,
                    guidance="Resolve the analyzer checkpoint conflict before retrying?",
                    span_id=span_id,
                )
            selected_aliases = checkpoint.selected_aliases
            retrieval_fingerprint = retrieval_checkpoint_fingerprint(
                source_fingerprint=source_fingerprint,
                final_turn_fingerprint=final_turn_fingerprint,
                selected_aliases=selected_aliases,
            )
            if retrieval_fingerprint != checkpoint.retrieval_fingerprint:
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=checkpoint,
                    failure_kind=AnalyzerFailureKind.ALIAS_VALIDATION,
                    recovery_action=AnalyzerRecoveryAction.CLARIFY,
                    guidance="Clarify the selected agenda decision before retrying this turn?",
                    span_id=span_id,
                )
            if checkpoint.stage == AnalyzerCheckpointStage.PENDING_MATERIALIZED:
                record = (
                    None
                    if checkpoint.proposal_ref is None
                    else self.store.proposal(checkpoint.proposal_ref)
                )
                if record is None or record.session_id != session_id:
                    return self._recover(
                        final_source_ref=final.final_source_ref,
                        checkpoint=None,
                        failure_kind=AnalyzerFailureKind.GOVERNANCE,
                        recovery_action=AnalyzerRecoveryAction.LOCK,
                        guidance="Resolve the missing validated proposal before retrying?",
                        span_id=span_id,
                    )
                if record.status != ProposalStatus.PENDING:
                    if span_id is not None:
                        self.telemetry.finish(span_id, outcome=SpanOutcome.OK)
                    return AnalyzerTurnResult(
                        schema_version=1,
                        turn_source_ref=final.final_source_ref,
                        finding_proposals=[],
                        complete_package_proposal=None,
                        control_intent=ControlIntent.NONE,
                        control_target=None,
                        target_proposal_ref=None,
                        edit_instruction=None,
                        acknowledgement="Proposal already resolved",
                        next_question=None,
                    )
                if (
                    record.base_foundation_revision
                    != session.expected_foundation_revision
                    or session.pending_proposal_ref != record.proposal_ref
                ):
                    return self._recover(
                        final_source_ref=final.final_source_ref,
                        checkpoint=None,
                        failure_kind=AnalyzerFailureKind.GOVERNANCE,
                        recovery_action=AnalyzerRecoveryAction.LOCK,
                        guidance="Resolve the governance blocker before retrying this turn?",
                        span_id=span_id,
                    )
                resumed = AnalyzerTurnResult.model_validate_json(
                    record.analyzer_result_json
                )
                validate_phase(resumed, session.conversation_phase)
                self._validate_grounding(resumed)
                self._validate_identities(session_id, resumed)
                if span_id is not None:
                    self.telemetry.finish(span_id, outcome=SpanOutcome.OK)
                return resumed

        if selected_aliases is None:
            try:
                deadline.ensure_hard_budget()
                candidates = DeterministicEvidenceRetriever(
                    self.evidence_index
                ).retrieve(final.normalized_text)
                selected_aliases = candidates.selected_aliases
                retrieval_fingerprint = retrieval_checkpoint_fingerprint(
                    source_fingerprint=source_fingerprint,
                    final_turn_fingerprint=candidates.final_turn_fingerprint,
                    selected_aliases=selected_aliases,
                )
                if candidates.outcome == RetrievalOutcome.NEEDS_CLARIFICATION:
                    checkpoint = self._new_checkpoint(
                        request_id=request_id,
                        session_id=session_id,
                        turn_sequence=turn_sequence,
                        request_fingerprint=request_fingerprint,
                        source_fingerprint=source_fingerprint,
                        retrieval_fingerprint=retrieval_fingerprint,
                        selected_aliases=(),
                    )
                    return self._recover(
                        final_source_ref=final.final_source_ref,
                        checkpoint=checkpoint,
                        failure_kind=AnalyzerFailureKind.ALIAS_VALIDATION,
                        recovery_action=AnalyzerRecoveryAction.CLARIFY,
                        guidance=candidates.clarification
                        or "Which agenda decision should ground this turn?",
                        span_id=span_id,
                    )
                AliasMaterializer(self.evidence_index).materialize(
                    case_id=session.case_id,
                    requested_aliases=selected_aliases,
                    selected_aliases=selected_aliases,
                    current_snapshots=self.evidence_snapshots,
                )
                deadline.ensure_hard_budget()
            except AnalyzerDeadlineExceeded:
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=None,
                    failure_kind=AnalyzerFailureKind.DEADLINE,
                    recovery_action=AnalyzerRecoveryAction.TEXT_FALLBACK,
                    guidance="Continue by text now, or retry this turn later?",
                    span_id=span_id,
                )
            except ValueError:
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=None,
                    failure_kind=AnalyzerFailureKind.ALIAS_VALIDATION,
                    recovery_action=AnalyzerRecoveryAction.CLARIFY,
                    guidance=(
                        "Clarify the selected agenda decision before retrying this turn?"
                    ),
                    span_id=span_id,
                )
            checkpoint = self._new_checkpoint(
                request_id=request_id,
                session_id=session_id,
                turn_sequence=turn_sequence,
                request_fingerprint=request_fingerprint,
                source_fingerprint=source_fingerprint,
                retrieval_fingerprint=retrieval_fingerprint,
                selected_aliases=selected_aliases,
            )
            self.store.save_analyzer_checkpoint(checkpoint)
        else:
            try:
                AliasMaterializer(self.evidence_index).materialize(
                    case_id=session.case_id,
                    requested_aliases=selected_aliases,
                    selected_aliases=selected_aliases,
                    current_snapshots=self.evidence_snapshots,
                )
            except ValueError:
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=checkpoint,
                    failure_kind=AnalyzerFailureKind.ALIAS_VALIDATION,
                    recovery_action=AnalyzerRecoveryAction.CLARIFY,
                    guidance="Clarify the selected agenda decision before retrying this turn?",
                    span_id=span_id,
                )

        if session.revision_locked:
            return self._recover(
                final_source_ref=final.final_source_ref,
                checkpoint=checkpoint,
                failure_kind=AnalyzerFailureKind.GOVERNANCE,
                recovery_action=AnalyzerRecoveryAction.LOCK,
                guidance="Resolve the governance blocker before retrying this turn?",
                span_id=span_id,
            )
        if not deadline.can_start_provider_call():
            return self._recover(
                final_source_ref=final.final_source_ref,
                checkpoint=checkpoint,
                failure_kind=AnalyzerFailureKind.DEADLINE,
                recovery_action=AnalyzerRecoveryAction.TEXT_FALLBACK,
                guidance="Continue by text now, or retry this turn later?",
                span_id=span_id,
            )

        selected = tuple(
            SelectedSemanticEvidence(
                alias=alias,
                display_label=self.evidence_index.unit_for(alias).display_label,
                text=self.evidence_index.unit_for(alias).text,
            )
            for alias in selected_aliases
        )
        request = SemanticAnalyzerRequest(
            schema_version=1,
            effort="medium",
            purpose=purpose,
            phase=session.conversation_phase,
            final_turn_text=final.normalized_text,
            business_context=self.business_context,
            candidates=selected,
            committed_context=committed_context,
            edit_instruction=edit_instruction,
            remaining_budget_ms=deadline.remaining_usable_ms,
        )
        prior_provider_calls = (
            0 if checkpoint is None else checkpoint.provider_call_count
        )
        result: AnalyzerTurnResult | None = None
        calls_this_session = 0
        last_failure = AnalyzerFailureKind.PROVIDER_AVAILABILITY
        last_action = AnalyzerRecoveryAction.REQUEUE
        last_guidance = "Continue by text, or retry this turn when analysis is available?"
        for attempt in range(1, 3):
            if not deadline.can_start_provider_call():
                last_failure = AnalyzerFailureKind.DEADLINE
                last_action = AnalyzerRecoveryAction.TEXT_FALLBACK
                last_guidance = "Continue by text now, or retry this turn later?"
                break
            calls_this_session += 1
            total_calls = prior_provider_calls + calls_this_session
            self.store.record_analyzer_attempt(
                request_id=request_id,
                session_id=session_id,
                turn_sequence=turn_sequence,
                effort=resolved_effort,
                status="IN_FLIGHT",
                attempt_count=total_calls,
                now=self.clock.now(),
            )
            try:
                raw = await deadline.run_provider(self.analyzer.analyze(request))
                if not deadline.within_usable_window():
                    raise AnalyzerDeadlineExceeded("provider cutoff exhausted")
                draft = SemanticTurnDraft.model_validate(
                    raw.model_dump(mode="python")
                )
            except AnalyzerDeadlineExceeded:
                last_failure = AnalyzerFailureKind.DEADLINE
                last_action = AnalyzerRecoveryAction.TEXT_FALLBACK
                last_guidance = "Continue by text now, or retry this turn later?"
            except (
                AnalyzerProviderAvailabilityError,
                ConnectionError,
                OSError,
                TimeoutError,
            ):
                last_failure = AnalyzerFailureKind.PROVIDER_AVAILABILITY
                last_action = AnalyzerRecoveryAction.REQUEUE
                last_guidance = (
                    "Continue by text, or retry this turn when analysis is available?"
                )
            except (AnalyzerProviderSchemaError, AttributeError, TypeError, ValueError):
                last_failure = AnalyzerFailureKind.PROVIDER_SCHEMA
                last_action = AnalyzerRecoveryAction.RETRY_TEXT
                last_guidance = "Retry this turn with the same selected evidence?"
            else:
                try:
                    result = self._assemble_semantic_result(
                        session_id=session_id,
                        final_source_ref=final.final_source_ref,
                        purpose=purpose,
                        phase=session.conversation_phase,
                        selected_aliases=selected_aliases,
                        draft=draft,
                    )
                    validate_phase(result, session.conversation_phase)
                    self._validate_grounding(result)
                    if result.complete_package_proposal is not None:
                        self._validate_identities(session_id, result)
                    if not deadline.within_usable_window():
                        raise AnalyzerDeadlineExceeded("usable deadline exhausted")
                except AnalyzerDeadlineExceeded:
                    result = None
                    last_failure = AnalyzerFailureKind.DEADLINE
                    last_action = AnalyzerRecoveryAction.TEXT_FALLBACK
                    last_guidance = "Continue by text now, or retry this turn later?"
                except Exception as exc:
                    result = None
                    last_failure = self._classify_local_failure(exc)
                    last_action, last_guidance = self._recovery_policy(last_failure)
                else:
                    break
            if (
                attempt == 1
                and last_failure
                in {
                    AnalyzerFailureKind.PROVIDER_AVAILABILITY,
                    AnalyzerFailureKind.PROVIDER_SCHEMA,
                    AnalyzerFailureKind.ALIAS_VALIDATION,
                }
                and await deadline.wait_for_retry()
            ):
                continue
            break

        if result is None:
            self.store.record_analyzer_attempt(
                request_id=request_id,
                session_id=session_id,
                turn_sequence=turn_sequence,
                effort=resolved_effort,
                status="FAILED",
                attempt_count=prior_provider_calls + calls_this_session,
                now=self.clock.now(),
            )
            recovery_checkpoint = checkpoint.model_copy(update={
                "provider_call_count": prior_provider_calls + calls_this_session,
                "updated_at": self.clock.now(),
            })
            return self._recover(
                final_source_ref=final.final_source_ref,
                checkpoint=recovery_checkpoint,
                failure_kind=last_failure,
                recovery_action=last_action,
                guidance=last_guidance,
                span_id=span_id,
            )

        total_calls = prior_provider_calls + calls_this_session
        if result.complete_package_proposal is not None:
            latest = self.store.latest_proposal(session_id)
            version = 1 if latest is None else latest.version + 1
            proposal_ref = (
                "workshop-patch-"
                f"{proposal_id(session_id, 'package', result.complete_package_proposal.proposal_key)}"
                f"-v{version}"
            )
            now = self.clock.now()
            proposal = PackageProposalRecord(
                proposal_ref=proposal_ref,
                session_id=session_id,
                version=version,
                base_foundation_revision=session.expected_foundation_revision,
                analyzer_result_json=result.model_dump_json(),
                status=ProposalStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            materialized_checkpoint = checkpoint.model_copy(update={
                "stage": AnalyzerCheckpointStage.PENDING_MATERIALIZED,
                "status": AnalyzerCheckpointStatus.VALIDATED,
                "failure_kind": None,
                "recovery_action": None,
                "proposal_ref": proposal_ref,
                "provider_call_count": total_calls,
                "updated_at": now,
            })
            try:
                self.store.save_validated_proposal_checkpoint(
                    proposal,
                    materialized_checkpoint,
                    commit_guard=deadline.ensure_hard_budget,
                )
            except AnalyzerDeadlineExceeded:
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=checkpoint.model_copy(update={
                        "provider_call_count": total_calls,
                        "updated_at": self.clock.now(),
                    }),
                    failure_kind=AnalyzerFailureKind.DEADLINE,
                    recovery_action=AnalyzerRecoveryAction.TEXT_FALLBACK,
                    guidance="Continue by text now, or retry this turn later?",
                    span_id=span_id,
                )
            except Exception:
                return self._recover(
                    final_source_ref=final.final_source_ref,
                    checkpoint=checkpoint.model_copy(update={
                        "provider_call_count": total_calls,
                        "updated_at": self.clock.now(),
                    }),
                    failure_kind=AnalyzerFailureKind.GOVERNANCE,
                    recovery_action=AnalyzerRecoveryAction.LOCK,
                    guidance="Resolve the governance blocker before retrying this turn?",
                    span_id=span_id,
                )
        elif result.control_intent in {
            ControlIntent.CONFIRM,
            ControlIntent.EDIT,
            ControlIntent.REJECT,
        }:
            self.apply_control(session_id, result, confirmation_context=True)

        self.store.record_analyzer_attempt(
            request_id=request_id,
            session_id=session_id,
            turn_sequence=turn_sequence,
            effort=resolved_effort,
            status="CONFIRMED",
            attempt_count=total_calls,
            now=self.clock.now(),
        )
        if span_id is not None:
            self.telemetry.finish(span_id, outcome=SpanOutcome.OK)
        return result

    def latest_recovery(self, session_id: UUID) -> AnalyzerRecoveryView | None:
        checkpoint = self.store.latest_analyzer_checkpoint(session_id)
        if (
            checkpoint is None
            or checkpoint.status != AnalyzerCheckpointStatus.RECOVERY
            or checkpoint.failure_kind is None
            or checkpoint.recovery_action is None
        ):
            return None
        _, guidance = self._recovery_policy(checkpoint.failure_kind)
        return AnalyzerRecoveryView(
            failure_kind=checkpoint.failure_kind,
            recovery_action=checkpoint.recovery_action,
            checkpoint_stage=checkpoint.stage,
            can_retry=checkpoint.recovery_action != AnalyzerRecoveryAction.LOCK,
            guidance=guidance,
        )

    def _new_checkpoint(
        self,
        *,
        request_id: UUID,
        session_id: UUID,
        turn_sequence: int,
        request_fingerprint: str,
        source_fingerprint: str,
        retrieval_fingerprint: str,
        selected_aliases: tuple[str, ...],
    ) -> AnalyzerCheckpoint:
        now = self.clock.now()
        return AnalyzerCheckpoint(
            request_id=request_id,
            session_id=session_id,
            turn_sequence=turn_sequence,
            request_fingerprint=request_fingerprint,
            source_fingerprint=source_fingerprint,
            retrieval_fingerprint=retrieval_fingerprint,
            selected_aliases=selected_aliases,
            stage=AnalyzerCheckpointStage.RETRIEVAL_VALIDATED,
            status=AnalyzerCheckpointStatus.VALIDATED,
            failure_kind=None,
            recovery_action=None,
            proposal_ref=None,
            provider_call_count=0,
            created_at=now,
            updated_at=now,
        )

    def _recover(
        self,
        *,
        final_source_ref,
        checkpoint: AnalyzerCheckpoint | None,
        failure_kind: AnalyzerFailureKind,
        recovery_action: AnalyzerRecoveryAction,
        guidance: str,
        span_id,
    ) -> AnalyzerTurnResult:
        if checkpoint is not None:
            recovery = checkpoint.model_copy(update={
                "stage": AnalyzerCheckpointStage.RETRIEVAL_VALIDATED,
                "status": AnalyzerCheckpointStatus.RECOVERY,
                "failure_kind": failure_kind,
                "recovery_action": recovery_action,
                "proposal_ref": None,
                "updated_at": self.clock.now(),
            })
            self.store.save_analyzer_checkpoint(recovery)
        if span_id is not None:
            self.telemetry.finish(
                span_id,
                outcome=(
                    SpanOutcome.CANCELLED
                    if failure_kind == AnalyzerFailureKind.DEADLINE
                    else SpanOutcome.ERROR
                ),
                error_code=f"ANALYZER_{failure_kind.value}",
            )
        acknowledgement = {
            AnalyzerFailureKind.PROVIDER_AVAILABILITY: "Analysis unavailable",
            AnalyzerFailureKind.PROVIDER_SCHEMA: "Analysis needs repair",
            AnalyzerFailureKind.ALIAS_VALIDATION: "Evidence validation failed",
            AnalyzerFailureKind.GROUNDING: "Grounding validation failed",
            AnalyzerFailureKind.GOVERNANCE: "Governance blocked analysis",
            AnalyzerFailureKind.DEADLINE: "Analysis deadline reached",
        }[failure_kind]
        return AnalyzerTurnResult(
            schema_version=1,
            turn_source_ref=final_source_ref,
            finding_proposals=[],
            complete_package_proposal=None,
            control_intent=ControlIntent.NONE,
            control_target=None,
            target_proposal_ref=None,
            edit_instruction=None,
            acknowledgement=acknowledgement,
            next_question=guidance,
        )

    @staticmethod
    def _classify_local_failure(exc: Exception) -> AnalyzerFailureKind:
        message = str(exc).casefold()
        if "ground" in message:
            return AnalyzerFailureKind.GROUNDING
        if any(
            token in message
            for token in (
                "alias",
                "evidence source",
                "evidence text",
                "supporting excerpt",
                "source snapshot",
                "cross-case",
            )
        ):
            return AnalyzerFailureKind.ALIAS_VALIDATION
        return AnalyzerFailureKind.GOVERNANCE

    @staticmethod
    def _recovery_policy(
        failure_kind: AnalyzerFailureKind,
    ) -> tuple[AnalyzerRecoveryAction, str]:
        return {
            AnalyzerFailureKind.PROVIDER_AVAILABILITY: (
                AnalyzerRecoveryAction.REQUEUE,
                "Continue by text, or retry this turn when analysis is available?",
            ),
            AnalyzerFailureKind.PROVIDER_SCHEMA: (
                AnalyzerRecoveryAction.RETRY_TEXT,
                "Retry this turn with the same selected evidence?",
            ),
            AnalyzerFailureKind.ALIAS_VALIDATION: (
                AnalyzerRecoveryAction.CLARIFY,
                "Clarify the selected agenda decision before retrying this turn?",
            ),
            AnalyzerFailureKind.GROUNDING: (
                AnalyzerRecoveryAction.CLARIFY,
                "Clarify the unsupported claim before retrying this turn?",
            ),
            AnalyzerFailureKind.GOVERNANCE: (
                AnalyzerRecoveryAction.LOCK,
                "Resolve the governance blocker before retrying this turn?",
            ),
            AnalyzerFailureKind.DEADLINE: (
                AnalyzerRecoveryAction.TEXT_FALLBACK,
                "Continue by text now, or retry this turn later?",
            ),
        }[failure_kind]

    def apply_control(
        self,
        session_id: UUID,
        control: AnalyzerTurnResult,
        *,
        confirmation_context: bool = False,
    ):
        session = self.store.get_session(session_id)
        validate_phase(control, session.conversation_phase)
        pending = self.store.pending_proposal(session_id)
        if control.control_intent == ControlIntent.REJECT:
            self._assert_target(control, pending)
            return self.store.set_proposal_status(pending.proposal_ref, ProposalStatus.REJECTED, self.clock.now())
        if control.control_intent == ControlIntent.EDIT:
            self._assert_target(control, pending)
            return self.store.set_proposal_status(pending.proposal_ref, ProposalStatus.SUPERSEDED, self.clock.now())
        if control.control_intent != ControlIntent.CONFIRM:
            return None
        if not confirmation_context:
            raise ValueError("confirmation requires an explicit pending-proposal prompt context")
        self._assert_target(control, pending)
        proposed = AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json).complete_package_proposal
        if proposed is None:
            raise ValueError("pending proposal has no complete package")
        self._validate_identities(session_id, AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json))
        payload, item_ids = self._materialize(session_id, proposed)
        package_id = proposal_id(session_id, "package", proposed.proposal_key)
        if proposed.existing_package_id is None:
            command = CreateSpecPackageV2Command(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:create-package"),
                case_id=session.case_id, acting_actor_id=session.pm_actor_id,
                expected_case_revision=session.expected_foundation_revision,
                package_id=package_id, content_schema_version=2, hash_schema_version=3, payload=payload,
            )
            created = self._commit(session_id, pending.proposal_ref, "create_spec_package", command)
        else:
            current = self.foundation.get_workflow_view(
                QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
            ).current_package
            if current is None:
                raise ValueError("existing package identity is not registered")
            command = ReviseSpecPackageV2Command(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:revise-package"),
                case_id=session.case_id, acting_actor_id=session.pm_actor_id,
                expected_case_revision=session.expected_foundation_revision,
                expected_artifact_id=current.artifact_id,
                expected_artifact_version=current.version,
                expected_artifact_hash=current.semantic_hash,
                content_schema_version=2,
                hash_schema_version=3,
                payload=payload,
            )
            created = self._commit(session_id, pending.proposal_ref, "revise_spec_package", command)
        revision = self._stored(created).receipt.revision
        self.store.set_foundation_revision(session_id, revision, self.clock.now())
        package_binding = self._stored(created).binding
        pending_result = AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json)
        blocked_item_ids: set[UUID] = set()
        for finding in pending_result.finding_proposals:
            if finding.disposition.value != "OPEN":
                continue
            item_id = item_ids.get(finding.item_proposal_key)
            if item_id is None:
                raise ValueError("finding references an unknown package item proposal key")
            finding_id = proposal_id(session_id, "finding", finding.proposal_key)
            finding_command = RecordItemAmbiguityFindingCommand(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:finding:{finding_id}"),
                case_id=session.case_id,
                acting_actor_id=session.pm_actor_id,
                expected_case_revision=revision,
                finding_id=finding_id,
                item_id=item_id,
                category=finding.category,
                domain=finding.domain,
                severity=finding.severity,
                evidence_refs=finding.evidence_refs,
                clarification_question=finding.clarification_question,
            )
            recorded = self._commit(
                session_id,
                f"{pending.proposal_ref}:finding:{finding_id}",
                "record_ambiguity_finding",
                finding_command,
            )
            revision = self._stored(recorded).receipt.revision
            blocked_item_ids.add(item_id)
            self.store.set_foundation_revision(session_id, revision, self.clock.now())
        governance = self.foundation.get_spec_package_governance(QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id))
        for item in governance.items:
            if item.binding.item_id in blocked_item_ids:
                continue
            mark_command = MarkSpecPackageItemReadyCommand(
                command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:mark:{item.binding.item_id}"),
                case_id=session.case_id, acting_actor_id=session.pm_actor_id, expected_case_revision=revision,
                expected_artifact_id=package_binding.artifact_id, expected_artifact_version=package_binding.version,
                expected_artifact_hash=package_binding.semantic_hash, item_binding=item.binding,
            )
            marked = self._commit(
                session_id,
                f"{pending.proposal_ref}:mark:{item.binding.item_id}",
                "mark_spec_package_item_ready",
                mark_command,
            )
            revision = self._stored(marked).receipt.revision
            scopes = [ApprovalScope.BUSINESS] if item.domain == Domain.BUSINESS else [ApprovalScope.TECHNICAL] if item.domain == Domain.TECHNICAL else [ApprovalScope.BUSINESS, ApprovalScope.TECHNICAL]
            for scope in scopes:
                approve_command = ApproveSpecPackageItemCommand(
                    command_id=uuid5(GATE_NAMESPACE, f"{pending.proposal_ref}:approve:{item.binding.item_id}:{scope.value}"),
                    case_id=session.case_id, acting_actor_id=session.pm_actor_id, expected_case_revision=revision,
                    expected_artifact_id=package_binding.artifact_id, expected_artifact_version=package_binding.version,
                    expected_artifact_hash=package_binding.semantic_hash, item_binding=item.binding, scope=scope,
                )
                approved = self._commit(
                    session_id,
                    f"{pending.proposal_ref}:approve:{item.binding.item_id}:{scope.value}",
                    "approve_spec_package_item",
                    approve_command,
                )
                revision = self._stored(approved).receipt.revision
            self.store.set_foundation_revision(session_id, revision, self.clock.now())
        return self.store.set_proposal_status(pending.proposal_ref, ProposalStatus.COMMITTED, self.clock.now())

    def _commit(self, session_id, proposal_ref, command_name, command):
        action = proposal_ref if command_name in {"create_spec_package", "revise_spec_package"} else proposal_ref
        return self.coordinator.commit_foundation_command(
            session_id,
            logical_action_key=f"gate:{command_name}:{action}",
            command_name=command_name,
            command=command,
        )

    def _validate_identities(self, session_id: UUID, result: AnalyzerTurnResult) -> None:
        proposal = result.complete_package_proposal
        for finding in result.finding_proposals:
            if finding.existing_finding_id is not None:
                raise ValueError("model supplied an unknown existing finding UUID")
        if proposal is None:
            return
        session = self.store.get_session(session_id)
        workflow = self.foundation.get_workflow_view(
            QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
        )
        committed_record = self.store.latest_proposal(session_id, status=ProposalStatus.COMMITTED)
        committed = None
        if committed_record is not None:
            committed = AnalyzerTurnResult.model_validate_json(
                committed_record.analyzer_result_json
            ).complete_package_proposal

        known: dict[tuple[str, str], UUID] = {}
        if committed is not None:
            known[("package", committed.proposal_key)] = (
                committed.existing_package_id
                or proposal_id(session_id, "package", committed.proposal_key)
            )
            for kind, values, field in (
                ("unit", [*committed.requirements, *committed.technical_decisions], "existing_unit_id"),
                ("check", committed.acceptance_checks, "existing_check_id"),
                ("item", committed.items, "existing_item_id"),
            ):
                for value in values:
                    known[(kind, value.proposal_key)] = getattr(value, field) or proposal_id(
                        session_id, kind, value.proposal_key
                    )

        def validate(kind: str, key: str, existing_id: UUID | None) -> None:
            recognized = known.get((kind, key))
            if existing_id is not None and existing_id != recognized:
                raise ValueError("model supplied a new or unknown UUID")
            if recognized is not None and existing_id != recognized:
                raise ValueError("committed proposal keys must retain their server UUID")

        validate("package", proposal.proposal_key, proposal.existing_package_id)
        if proposal.existing_package_id is not None and (
            workflow.current_package is None
            or workflow.current_package.artifact_id != proposal.existing_package_id
        ):
            raise ValueError("model supplied an unknown package UUID")
        for value in [*proposal.requirements, *proposal.technical_decisions]:
            validate("unit", value.proposal_key, value.existing_unit_id)
        for value in proposal.acceptance_checks:
            validate("check", value.proposal_key, value.existing_check_id)
        for value in proposal.items:
            validate("item", value.proposal_key, value.existing_item_id)

        if committed is not None:
            old_keys = {
                value.proposal_key
                for value in [
                    *committed.requirements,
                    *committed.technical_decisions,
                    *committed.acceptance_checks,
                    *committed.items,
                ]
            }
            new_keys = {
                value.proposal_key
                for value in [
                    *proposal.requirements,
                    *proposal.technical_decisions,
                    *proposal.acceptance_checks,
                    *proposal.items,
                ]
            }
            if not old_keys.issubset(new_keys):
                raise ValueError("package proposal is a partial collection")

    @staticmethod
    def _stored(result): return result.stored_result if not result.mutated else result

    @staticmethod
    def _assert_target(control, pending):
        if pending is None or control.target_proposal_ref != pending.proposal_ref:
            raise ValueError("control target is not the single visible pending proposal")

    def _validate_grounding(self, result: AnalyzerTurnResult) -> None:
        claims = []
        for finding in result.finding_proposals:
            claims.append((finding.clarification_question, tuple(finding.evidence_refs)))
        proposal = result.complete_package_proposal
        if proposal:
            for value in [*proposal.requirements, *proposal.technical_decisions, *proposal.acceptance_checks]:
                claims.append((value.statement, tuple(value.source_refs)))
        for claim, refs in claims:
            if not self.grounding.supports(claim, refs):
                code = getattr(self.grounding, "last_failure_code", None)
                suffix = f": {code}" if isinstance(code, str) else ""
                raise ValueError(
                    "analyzer proposal is not grounded in its cited evidence" + suffix
                )

    def _current_content(self, session):
        try:
            return self.foundation.get_spec_package_content(
                QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
            )
        except Exception:
            return None

    def _semantic_committed_context(self, session) -> CommittedSemanticContext:
        content = self._current_content(session)
        if content is None:
            return CommittedSemanticContext(package=None)
        payload = content.payload
        if not (
            len(payload.items) == 1
            and len(payload.requirements) == 1
            and len(payload.technical_decisions) == 1
            and len(payload.acceptance_checks) == 1
        ):
            raise ValueError("Workshop v0 semantic analyzer supports one complete governed item")
        return CommittedSemanticContext(
            package=CommittedPackageSemantics(
                item_title=payload.items[0].title,
                business_requirement=payload.requirements[0].statement,
                technical_decision=payload.technical_decisions[0].statement,
                acceptance_check=payload.acceptance_checks[0].statement,
            )
        )

    def _assemble_semantic_result(
        self,
        *,
        session_id: UUID,
        final_source_ref,
        purpose: str,
        phase,
        selected_aliases: tuple[str, ...],
        draft: SemanticTurnDraft,
    ) -> AnalyzerTurnResult:
        if purpose == "FINISH_AUDIT" and draft.package_delta is not None:
            raise ValueError("finish audit cannot return a package delta")
        if phase.value != "WORKSHOP" and draft.package_delta is not None:
            raise ValueError("package semantic deltas are WORKSHOP-only")
        if purpose == "FINISH_AUDIT" and draft.control_intent != ControlIntent.NONE:
            raise ValueError("finish audit cannot return a control intent")

        grounded = [value.question for value in draft.findings]
        if draft.package_delta is not None:
            grounded.extend(
                (
                    draft.package_delta.business_requirement,
                    draft.package_delta.technical_decision,
                    draft.package_delta.acceptance_check,
                )
            )
        selected = set(selected_aliases)
        units = {value.alias: value for value in self.evidence_index.units}
        requested: set[str] = set()
        for value in grounded:
            for alias in value.evidence_aliases:
                if alias not in selected or alias not in units:
                    raise ValueError("semantic draft cited an unknown or unselected alias")
                requested.add(alias)
            for support in value.supporting_excerpts:
                if support.alias not in selected or support.alias not in units:
                    raise ValueError("semantic draft excerpt cited an unknown or unselected alias")
                if support.excerpt not in units[support.alias].text:
                    raise ValueError("semantic supporting excerpt is not exact evidence text")
        refs = ()
        if requested:
            refs = AliasMaterializer(self.evidence_index).materialize(
                case_id=self.store.get_session(session_id).case_id,
                requested_aliases=tuple(sorted(requested)),
                selected_aliases=selected_aliases,
                current_snapshots=self.evidence_snapshots,
            )
        refs_by_alias = dict(zip(sorted(requested), refs, strict=True))

        def source_refs(value: GroundedSemanticText):
            return [refs_by_alias[alias] for alias in value.evidence_aliases]

        session = self.store.get_session(session_id)
        package = self._assemble_package(
            session_id, session, draft.package_delta, source_refs
        )
        item_key = "item-001"
        if package is not None:
            item_key = package.items[0].proposal_key
        else:
            committed = self.store.latest_proposal(
                session_id, status=ProposalStatus.COMMITTED
            )
            if committed is not None:
                prior = AnalyzerTurnResult.model_validate_json(
                    committed.analyzer_result_json
                ).complete_package_proposal
                if prior is not None:
                    item_key = prior.items[0].proposal_key

        def owners(domain: Domain) -> list[UUID]:
            if domain == Domain.BUSINESS:
                return [session.pm_actor_id]
            if domain == Domain.TECHNICAL:
                return [self.dev_lead_actor_id]
            return [session.pm_actor_id, self.dev_lead_actor_id]

        findings = [
            FindingProposal(
                proposal_key=f"finding-{index:03d}",
                existing_finding_id=None,
                item_proposal_key=item_key,
                category=AmbiguityCategory.MISSING_TECH_DECISION,
                domain=value.domain,
                severity=Severity.BLOCKING,
                evidence_refs=source_refs(value.question),
                clarification_question=value.question.text,
                owner_actor_ids=owners(
                    Domain.CROSS_DOMAIN if package is not None else value.domain
                ),
                disposition=ProposalDisposition.OPEN,
            )
            for index, value in enumerate(draft.findings, start=1)
        ]
        control_target = None
        target_proposal_ref = None
        if draft.control_intent in {
            ControlIntent.CONFIRM,
            ControlIntent.EDIT,
            ControlIntent.REJECT,
        }:
            pending = self.store.pending_proposal(session_id)
            if pending is None:
                raise ValueError("semantic control requires one visible pending proposal")
            control_target = ControlTarget.WORKSHOP_PATCH
            target_proposal_ref = pending.proposal_ref
        next_question = draft.next_question
        if next_question is None and findings:
            next_question = findings[0].clarification_question
        return AnalyzerTurnResult(
            schema_version=1,
            turn_source_ref=final_source_ref,
            finding_proposals=findings,
            complete_package_proposal=package,
            control_intent=draft.control_intent,
            control_target=control_target,
            target_proposal_ref=target_proposal_ref,
            edit_instruction=draft.edit_instruction,
            acknowledgement=draft.acknowledgement,
            next_question=next_question,
        )

    def _assemble_package(self, session_id, session, delta, source_refs):
        if delta is None:
            return None
        content = self._current_content(session)
        if content is not None and not (
            len(content.payload.items) == 1
            and len(content.payload.requirements) == 1
            and len(content.payload.technical_decisions) == 1
            and len(content.payload.acceptance_checks) == 1
        ):
            raise ValueError("Workshop v0 local assembler supports one complete governed item")
        prior_record = self.store.latest_proposal(
            session_id, status=ProposalStatus.COMMITTED
        )
        prior = None
        if prior_record is not None:
            prior = AnalyzerTurnResult.model_validate_json(
                prior_record.analyzer_result_json
            ).complete_package_proposal
        package_key = prior.proposal_key if prior is not None else "package-001"
        requirement_key = (
            prior.requirements[0].proposal_key if prior is not None else "requirement-001"
        )
        decision_key = (
            prior.technical_decisions[0].proposal_key
            if prior is not None
            else "technical-decision-001"
        )
        check_key = (
            prior.acceptance_checks[0].proposal_key
            if prior is not None
            else "acceptance-check-001"
        )
        item_key = prior.items[0].proposal_key if prior is not None else "item-001"
        payload = None if content is None else content.payload
        requirement_id = None if payload is None else payload.requirements[0].unit_id
        decision_id = None if payload is None else payload.technical_decisions[0].unit_id
        check_id = None if payload is None else payload.acceptance_checks[0].check_id
        item_id = None if payload is None else payload.items[0].item_id
        package_id = None if content is None else content.package_binding.artifact_id
        return CompletePackageProposal(
            proposal_key=package_key,
            existing_package_id=package_id,
            requirements=[
                RequirementProposal(
                    proposal_key=requirement_key,
                    existing_unit_id=requirement_id,
                    statement=delta.business_requirement.text,
                    domain=Domain.BUSINESS,
                    delivery_required=True,
                    source_refs=source_refs(delta.business_requirement),
                )
            ],
            technical_decisions=[
                TechnicalDecisionProposal(
                    proposal_key=decision_key,
                    existing_unit_id=decision_id,
                    statement=delta.technical_decision.text,
                    domain=Domain.TECHNICAL,
                    delivery_required=True,
                    source_refs=source_refs(delta.technical_decision),
                )
            ],
            acceptance_checks=[
                AcceptanceCheckProposal(
                    proposal_key=check_key,
                    existing_check_id=check_id,
                    statement=delta.acceptance_check.text,
                    domain=Domain.CROSS_DOMAIN,
                    related_unit_proposal_keys=[requirement_key, decision_key],
                    source_refs=source_refs(delta.acceptance_check),
                )
            ],
            items=[
                SpecPackageItemProposal(
                    proposal_key=item_key,
                    existing_item_id=item_id,
                    title=delta.item_title,
                    requirement_proposal_keys=[requirement_key],
                    technical_decision_proposal_keys=[decision_key],
                    acceptance_check_proposal_keys=[check_key],
                    dependency_item_proposal_keys=[],
                )
            ],
        )

    @staticmethod
    def _materialize(session_id, proposed):
        units = {value.proposal_key: (value.existing_unit_id or proposal_id(session_id, "unit", value.proposal_key)) for value in [*proposed.requirements, *proposed.technical_decisions]}
        checks = {value.proposal_key: (value.existing_check_id or proposal_id(session_id, "check", value.proposal_key)) for value in proposed.acceptance_checks}
        items = {value.proposal_key: (value.existing_item_id or proposal_id(session_id, "item", value.proposal_key)) for value in proposed.items}
        payload = SpecPackagePayloadV2(
            requirements=[Requirement(unit_id=units[v.proposal_key], statement=v.statement, domain=v.domain, delivery_required=v.delivery_required, source_refs=v.source_refs) for v in proposed.requirements],
            technical_decisions=[TechnicalDecision(unit_id=units[v.proposal_key], statement=v.statement, domain=v.domain, delivery_required=v.delivery_required, source_refs=v.source_refs, provisional=False) for v in proposed.technical_decisions],
            acceptance_checks=[AcceptanceCheck(check_id=checks[v.proposal_key], statement=v.statement, domain=v.domain, related_unit_ids=[units[k] for k in v.related_unit_proposal_keys], source_refs=v.source_refs) for v in proposed.acceptance_checks],
            items=[SpecPackageItem(item_id=items[v.proposal_key], title=v.title, requirement_ids=[units[k] for k in v.requirement_proposal_keys], technical_decision_ids=[units[k] for k in v.technical_decision_proposal_keys], acceptance_check_ids=[checks[k] for k in v.acceptance_check_proposal_keys], dependency_item_ids=[items[k] for k in v.dependency_item_proposal_keys]) for v in proposed.items],
        )
        return payload, items
