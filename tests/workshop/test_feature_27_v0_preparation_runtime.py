from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from specops_contracts import workshop_v1 as c
from specops_workflow import migrate
from specops_workflow.persistence import WORKSHOP_PROTOCOL_TABLES, V0_RUNTIME_TABLES, engine_for
from specops_workflow.workshop_protocol import FoundationProtocolError
from specops_workflow.workshop_completion import (
    CompletionUtterance,
    classify_completion_utterance,
)
from specops_workshop.api import create_app
from specops_workshop.sources import SourceCatalog
from specops_workshop.v4.live_transport import V4LiveTransport
from specops_workshop.v4.openai_adapter import (
    BootstrapResult,
    PreparedProviderContext,
    ProviderAdapterError,
    ProviderCleanupReceipt,
    ProviderResourceDeletion,
)
from specops_workshop.v4.orchestrator import FinalTranscriptInput, V4ProductionOrchestrator
from specops_workshop.v4.scheduler import DurableAnalyzerWorker

from test_feature_25_v4_foundation import (
    CASE_ID,
    CONTEXT_ID,
    NOW,
    REQUEST_HASH,
    RUN_ID,
    SOURCE_SET_HASH,
    _activate,
    _base,
    _runtime,
    _transcript_command,
)
from test_feature_26_v4_production_seam import (
    ROOT,
    DeterministicAdapter,
    configured,
)


def _brief(*, unsafe_question_index: int | None = None) -> c.InterviewBriefCandidate:
    questions = tuple(
        c.QuestionCandidate(
            candidate_key=f"question-{index}",
            text=f"Which confirmed export choice applies to clarification area {index}?",
            rationale=f"Clarification area {index} must be answered independently.",
            question_shape=c.QuestionShape.OPEN_TEXT,
            capture_policy=c.CapturePolicy.CLARIFICATION_ONLY,
            answer_options=(),
            addresses_problem_keys=("problem-export",),
            prerequisite_problem_keys=(),
            safe_without_current_turn_interpretation=index != unsafe_question_index,
        )
        for index in range(1, c.INITIAL_RUNWAY_DEPTH + 1)
    )
    return c.InterviewBriefCandidate(
        protocol_version="1.0.0",
        output_type="INTERVIEW_BRIEF_CANDIDATE",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        customer_promise_summary="Export filtered orders with governed behavior.",
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-export",
                source_role=c.SourceRole.TECHNICAL_CONTRACT,
                locator=c.SourceLineLocator(
                    locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                    start_line=531,
                    end_line=531,
                ),
                relevance_claim="The source fixes the CSV encoding contract.",
                quoted_text_candidate="CSV output uses UTF-8 and one header row.",
            ),
        ),
        problems=(
            c.ProblemCandidate(
                candidate_key="problem-export",
                problem_kind=c.ProblemKind.MISSING_DECISION,
                domain=c.Domain.PRODUCT,
                severity=c.Severity.HIGH,
                statement="The PM must close the export behavior decisions.",
                consequence="The governed package cannot be completed yet.",
                evidence_candidate_keys=("evidence-export",),
            ),
        ),
        problem_clusters=(),
        questions=questions,
        initial_runway=c.QuestionRunwayCandidate(
            recommended_question_key="question-1",
            safe_alternate_question_keys=tuple(
                f"question-{index}" for index in range(2, c.INITIAL_RUNWAY_DEPTH + 1)
            ),
            do_not_ask_question_keys=(),
        ),
        confirmation_checkpoints=(),
    )


def _bootstrap_provider_error(
    code: c.ProviderFailureCode,
    *,
    correction_code: str | None = None,
) -> ProviderAdapterError:
    return ProviderAdapterError(
        c.ProviderFailureReceipt(
            provider=c.ProviderName.OPENAI,
            stage=(
                c.ProviderProcessingStage.LOCAL_VALIDATION
                if code is c.ProviderFailureCode.OUTPUT_INVALID
                else c.ProviderProcessingStage.BOOTSTRAP
            ),
            client_request_id="client-bootstrap-safe-failure",
            provider_request_id="req-bootstrap-safe-failure",
            status_code=None,
            code=code,
            retryable=code is not c.ProviderFailureCode.OUTPUT_INVALID,
            validation_diagnostics=(),
            occurred_at=NOW,
        ),
        bootstrap_correction_code=correction_code,
    )


def _admit_brief(foundation, *, unsafe_question_index: int | None = None):
    values = _base(4)
    values.update(
        command_type="ADMIT_INTERVIEW_BRIEF",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        provider_request_hash=REQUEST_HASH,
        candidate=_brief(unsafe_question_index=unsafe_question_index),
    )
    return foundation.execute(c.AdmitInterviewBriefCommand(**values))


def _seed_invalid_turn_correction(foundation):
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    foundation.execute(
        _transcript_command(
            foundation.case_revision(CASE_ID),
            1,
            "This finalized answer produces one quarantined branch.",
        )
    )
    parent = foundation.claim_analyzer_job(CASE_ID, worker_id="crashed-primary-worker")
    context = foundation.active_analyzer_context(CASE_ID)
    candidate = c.TurnAnalysisCandidate(
        protocol_version="1.0.0",
        output_type="TURN_ANALYSIS_CANDIDATE",
        analyzer_run_id=uuid4(),
        context_id=context.context_id,
        request_hash="sha256:" + "6" * 64,
        source_set_hash=SOURCE_SET_HASH,
        transcript_event_id=UUID(parent["subject_id"]),
        based_on_case_revision=foundation.case_revision(CASE_ID),
        disposition=c.TurnDisposition.SUBSTANTIVE,
        no_change_reason_code=None,
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-invalid-only",
                source_role=c.SourceRole.PM_SPEC,
                locator=c.QuoteSearchLocator(
                    locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
                    exact_quote="A nonexistent exact quote.",
                    occurrence=1,
                ),
                relevance_claim="This branch is deterministically invalid.",
                quoted_text_candidate="A nonexistent exact quote.",
            ),
        ),
        new_problems=(),
        new_problem_clusters=(),
        revised_problem_clusters=(),
        new_questions=(),
        revised_questions=(),
        low_risk_facts=(),
        decisions=(),
        problem_assessments=(),
        evidence_findings=(),
    )
    foundation.checkpoint_analyzer_job(
        parent["job_id"],
        worker_id="crashed-primary-worker",
        state="PROVIDER_COMPLETED",
        candidate_json=candidate.model_dump_json(),
    )
    correction = foundation.enqueue_turn_analysis_correction(
        parent["job_id"], worker_id="crashed-primary-worker"
    )
    foundation.checkpoint_analyzer_job(
        parent["job_id"],
        worker_id="crashed-primary-worker",
        state="COMPLETED",
    )
    return context, correction


def test_real_0004_database_upgrades_additively_to_0007(tmp_path):
    url, original = _runtime(tmp_path)
    original_case = original.get_case(CASE_ID)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    # Preserve a real canonical V4 case while removing only the new operational
    # tables. Registration after upgrade must backfill those rows idempotently.
    command.downgrade(config, "0004")
    with engine_for(url).connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0004"
        assert "workshop_preparations" not in engine_for(url).dialect.get_table_names(connection)
    migrate(url)
    with engine_for(url).connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0007"
        tables = set(engine_for(url).dialect.get_table_names(connection))
    assert {
        "workshop_preparations",
        "workshop_preparation_resources",
        "workshop_runway_items",
        "workshop_analyzer_jobs",
        "workshop_provider_responses",
    }.issubset(tables)
    restarted = type(original)(url, now=lambda: NOW)
    restarted.register_case(
        case_id=original_case.case_id,
        session_id=original_case.session_id,
        source_set_hash=original_case.source_set_hash,
    )
    assert restarted.preparation_projection(CASE_ID)["phase"] == "VALIDATING_DOCUMENTS"
    assert restarted.preparation_resources(CASE_ID)["source_set_hash"] == SOURCE_SET_HASH


def test_post_bootstrap_response_identity_is_durable_idempotent_and_clearable(tmp_path):
    _, foundation = _runtime(tmp_path)
    values = {
        "client_request_id": "specops-turn_analysis-durable",
        "operation": "TURN_ANALYSIS",
        "provider_response_id": "resp_turn_durable",
    }
    foundation.checkpoint_provider_response(CASE_ID, **values)
    foundation.checkpoint_provider_response(CASE_ID, **values)
    pending = foundation.pending_provider_responses(CASE_ID)
    assert len(pending) == 1
    assert pending[0]["provider_response_id"] == "resp_turn_durable"
    with pytest.raises(RuntimeError, match="identity conflict"):
        foundation.checkpoint_provider_response(
            CASE_ID,
            client_request_id=values["client_request_id"],
            operation=values["operation"],
            provider_response_id="resp_conflict",
        )
    foundation.clear_provider_response(CASE_ID, "resp_turn_durable")
    assert foundation.pending_provider_responses(CASE_ID) == ()


def test_analyzer_worker_checkpoints_stored_response_before_admission(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    context = foundation.active_analyzer_context(CASE_ID)

    class CheckpointingAdapter(DeterministicAdapter):
        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute_with_response_checkpoint(
            self, request, *, context, checkpoint
        ):
            checkpoint("resp_turn_worker")
            return await self.execute(request, context=context)

    adapter = CheckpointingAdapter()
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    asyncio.run(
        orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=1,
                text="The finalized turn must retain its stored Response identity.",
                provider_request_id="task-27-response-checkpoint",
                speaker_actor_id=foundation.case_actor(CASE_ID, "PM"),
                actor="PM",
            )
        )
    )
    worker = DurableAnalyzerWorker(orchestrator, worker_id="response-checkpoint-worker")
    assert asyncio.run(worker.run_once()) is True
    assert foundation.analyzer_jobs(CASE_ID)[0]["state"] == "COMPLETED"
    pending = foundation.pending_provider_responses(CASE_ID)
    assert len(pending) == 1
    assert pending[0]["operation"] == "TURN_ANALYSIS"
    assert pending[0]["provider_response_id"] == "resp_turn_worker"


def test_verified_turn_branch_replenishes_before_one_bounded_correction(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    context = foundation.active_analyzer_context(CASE_ID)
    actor = foundation.case_actor(CASE_ID, "PM")
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=SimpleNamespace(source_set_hash=lambda sources: SOURCE_SET_HASH),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    for sequence in range(1, 3):
        asyncio.run(
            orchestrator.record_final_transcript(
                FinalTranscriptInput(
                    turn_sequence=sequence,
                    text=f"Final answer {sequence}.",
                    provider_request_id=f"partitioned-turn-{sequence}",
                    speaker_actor_id=actor,
                    actor="PM",
                )
            )
        )

    class PartitionedTurnAdapter:
        calls = []
        correction_calls = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute(self, request, *, context):
            self.calls.append(request.request_type)
            if isinstance(request, c.ReplenishGuidanceRequest):
                question = next(
                    item
                    for item in request.foundation_snapshot.questions
                    if item.text == "Which verified delivery option should be confirmed?"
                )
                return c.GuidanceCandidate(
                    protocol_version="1.0.0",
                    output_type="GUIDANCE_CANDIDATE",
                    analyzer_run_id=request.analyzer_run_id,
                    context_id=request.context_id,
                    request_hash=request.request_hash,
                    source_set_hash=request.source_set_hash,
                    based_on_case_revision=request.based_on_case_revision,
                    recommended_question=c.GuidanceQuestion(
                        question_ref=c.FoundationEntityRef(
                            ref_kind="FOUNDATION_ID",
                            foundation_id=question.question_id,
                            expected_version=question.question_version,
                        ),
                        exact_text=question.text,
                        reason=question.rationale,
                    ),
                    safe_alternates=(),
                    do_not_ask_question_refs=(),
                    dependencies=(
                        c.GuidanceDependency(
                            dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                            entity_ref=None,
                        ),
                    ),
                    acknowledgement_suggestion="A verified clarification is ready.",
                )
            assert isinstance(request, c.AnalyzeFinalTurnRequest)
            common = dict(
                protocol_version="1.0.0",
                output_type="TURN_ANALYSIS_CANDIDATE",
                analyzer_run_id=request.analyzer_run_id,
                context_id=request.context_id,
                request_hash=request.request_hash,
                source_set_hash=request.source_set_hash,
                transcript_event_id=request.transcript.transcript_event_id,
                based_on_case_revision=request.based_on_case_revision,
                new_problems=(),
                new_problem_clusters=(),
                revised_problem_clusters=(),
                new_questions=(),
                revised_questions=(),
                low_risk_facts=(),
                decisions=(),
                problem_assessments=(),
                evidence_findings=(),
            )
            if request.transcript.sequence_number == 1:
                valid_evidence = c.CandidateEntityRef(
                    ref_kind="CANDIDATE_KEY", candidate_key="evidence-valid"
                )
                invalid_evidence = c.CandidateEntityRef(
                    ref_kind="CANDIDATE_KEY", candidate_key="evidence-invalid"
                )
                valid_problem = c.CandidateEntityRef(
                    ref_kind="CANDIDATE_KEY", candidate_key="problem-valid"
                )
                invalid_problem = c.CandidateEntityRef(
                    ref_kind="CANDIDATE_KEY", candidate_key="problem-invalid"
                )
                return c.TurnAnalysisCandidate(
                    **{
                        **common,
                        "new_problems": (
                            c.TurnProblemCandidate(
                                candidate_key="problem-valid",
                                problem_kind=c.ProblemKind.MISSING_DECISION,
                                domain=c.Domain.PRODUCT,
                                severity=c.Severity.HIGH,
                                statement="A verified delivery choice remains open.",
                                consequence="The delivery contract remains incomplete.",
                                evidence_refs=(valid_evidence,),
                            ),
                            c.TurnProblemCandidate(
                                candidate_key="problem-invalid",
                                problem_kind=c.ProblemKind.MISSING_DECISION,
                                domain=c.Domain.PRODUCT,
                                severity=c.Severity.HIGH,
                                statement="An ungrounded delivery choice was proposed.",
                                consequence="It must not enter the live runway.",
                                evidence_refs=(invalid_evidence,),
                            ),
                        ),
                        "new_questions": (
                            c.TurnQuestionCandidate(
                                candidate_key="question-valid",
                                text="Which verified delivery option should be confirmed?",
                                rationale="The verified problem needs a participant decision.",
                                question_shape=c.QuestionShape.OPEN_TEXT,
                                capture_policy=c.CapturePolicy.CLARIFICATION_ONLY,
                                answer_options=(),
                                addresses_problem_refs=(valid_problem,),
                                prerequisite_problem_refs=(),
                                safe_without_current_turn_interpretation=True,
                            ),
                            c.TurnQuestionCandidate(
                                candidate_key="question-invalid",
                                text="Which ungrounded delivery option should be confirmed?",
                                rationale="This branch must remain quarantined.",
                                question_shape=c.QuestionShape.OPEN_TEXT,
                                capture_policy=c.CapturePolicy.CLARIFICATION_ONLY,
                                answer_options=(),
                                addresses_problem_refs=(invalid_problem,),
                                prerequisite_problem_refs=(),
                                safe_without_current_turn_interpretation=True,
                            ),
                        ),
                    },
                    disposition=c.TurnDisposition.SUBSTANTIVE,
                    no_change_reason_code=None,
                    evidence_candidates=(
                        c.EvidenceCandidate(
                            candidate_key="evidence-valid",
                            source_role=c.SourceRole.PM_SPEC,
                            locator=c.SourceLineLocator(
                                locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                                start_line=1,
                                end_line=1,
                            ),
                            relevance_claim="The source grounds the verified branch.",
                            quoted_text_candidate="Export filtered orders.",
                        ),
                        c.EvidenceCandidate(
                            candidate_key="evidence-invalid",
                            source_role=c.SourceRole.PM_SPEC,
                            locator=c.QuoteSearchLocator(
                                locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
                                exact_quote="This alleged exact quote is absent.",
                                occurrence=1,
                            ),
                            relevance_claim="The proposed quote would ground this turn.",
                            quoted_text_candidate="This alleged exact quote is absent.",
                        ),
                    ),
                )
            return c.TurnAnalysisCandidate(
                **common,
                disposition=c.TurnDisposition.NO_SEMANTIC_CHANGE,
                no_change_reason_code="SOCIAL_ONLY",
                evidence_candidates=(),
            )

        async def execute_turn_analysis_correction_with_response_checkpoint(
            self,
            request,
            *,
            context,
            quarantined_candidate_keys,
            checkpoint,
        ):
            self.correction_calls += 1
            assert set(quarantined_candidate_keys) == {
                "evidence-invalid",
                "problem-invalid",
                "question-invalid",
            }
            checkpoint("resp_turn_correction")
            evidence = c.CandidateEntityRef(
                ref_kind="CANDIDATE_KEY", candidate_key="evidence-invalid"
            )
            problem = c.CandidateEntityRef(
                ref_kind="CANDIDATE_KEY", candidate_key="problem-invalid"
            )
            return c.TurnAnalysisCandidate(
                protocol_version="1.0.0",
                output_type="TURN_ANALYSIS_CANDIDATE",
                analyzer_run_id=request.analyzer_run_id,
                context_id=request.context_id,
                request_hash=request.request_hash,
                source_set_hash=request.source_set_hash,
                transcript_event_id=request.transcript.transcript_event_id,
                based_on_case_revision=request.based_on_case_revision,
                disposition=c.TurnDisposition.SUBSTANTIVE,
                no_change_reason_code=None,
                evidence_candidates=(
                    c.EvidenceCandidate(
                        candidate_key="evidence-invalid",
                        source_role=c.SourceRole.PM_SPEC,
                        locator=c.SourceLineLocator(
                            locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                            start_line=1,
                            end_line=1,
                        ),
                        relevance_claim="The corrected branch now has exact source evidence.",
                        quoted_text_candidate="Export filtered orders.",
                    ),
                ),
                new_problems=(
                    c.TurnProblemCandidate(
                        candidate_key="problem-invalid",
                        problem_kind=c.ProblemKind.MISSING_DECISION,
                        domain=c.Domain.PRODUCT,
                        severity=c.Severity.HIGH,
                        statement="A corrected evidence-bound choice remains open.",
                        consequence="The participant must confirm the grounded choice.",
                        evidence_refs=(evidence,),
                    ),
                ),
                new_problem_clusters=(),
                revised_problem_clusters=(),
                new_questions=(
                    c.TurnQuestionCandidate(
                        candidate_key="question-invalid",
                        text="Which corrected evidence-bound option should be confirmed?",
                        rationale="The corrected branch is now safe for later selection.",
                        question_shape=c.QuestionShape.OPEN_TEXT,
                        capture_policy=c.CapturePolicy.CLARIFICATION_ONLY,
                        answer_options=(),
                        addresses_problem_refs=(problem,),
                        prerequisite_problem_refs=(),
                        safe_without_current_turn_interpretation=True,
                    ),
                ),
                revised_questions=(),
                low_risk_facts=(),
                decisions=(),
                problem_assessments=(),
                evidence_findings=(),
            )

    adapter = PartitionedTurnAdapter()
    orchestrator.adapter = adapter
    worker = DurableAnalyzerWorker(orchestrator, worker_id="partition-worker")
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is True
    jobs_before_guidance = foundation.analyzer_jobs(CASE_ID)
    correction = next(
        item for item in jobs_before_guidance if item["subject_id"].startswith("turn-correction:")
    )
    assert correction["state"] == "ANALYSIS_PENDING"
    assert foundation.runway_projection(CASE_ID)["depth"] == 2

    # A process restart reconstructs both the verified Foundation subset and
    # the still-pending correction without another primary provider call.
    foundation = type(foundation)(url, now=lambda: NOW)
    context = foundation.active_analyzer_context(CASE_ID)
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    worker = DurableAnalyzerWorker(orchestrator, worker_id="restarted-partition-worker")

    # GUIDANCE (priority 20) consumes the verified subset before correction
    # (priority 30), preserving the two live questions and appending the new one.
    assert asyncio.run(worker.run_once()) is True
    assert adapter.correction_calls == 0
    runway = foundation.runway_projection(CASE_ID)
    assert runway["depth"] == 3
    assert [item["exact_text"] for item in runway["questions"]][-1] == (
        "Which verified delivery option should be confirmed?"
    )

    snapshot = foundation.semantic_snapshot(CASE_ID)
    assert any(
        item.text == "Which verified delivery option should be confirmed?"
        for item in snapshot.questions
    )
    assert all(
        item.text != "Which ungrounded delivery option should be confirmed?"
        for item in snapshot.questions
    )

    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False

    jobs = foundation.analyzer_jobs(CASE_ID)
    assert all(item["state"] == "COMPLETED" for item in jobs)
    assert adapter.correction_calls == 1
    corrected_snapshot = foundation.semantic_snapshot(CASE_ID)
    assert any(
        item.text == "Which corrected evidence-bound option should be confirmed?"
        for item in corrected_snapshot.questions
    )
    assert foundation.runway_projection(CASE_ID)["depth"] == 3
    assert adapter.calls == [
        c.AnalyzerOperation.TURN_ANALYSIS,
        c.AnalyzerOperation.TURN_ANALYSIS,
        c.AnalyzerOperation.GUIDANCE,
    ]


def test_turn_correction_failure_is_terminal_without_retry_or_runway_loss(tmp_path):
    _, foundation = _runtime(tmp_path)
    context, _ = _seed_invalid_turn_correction(foundation)
    depth_before = foundation.runway_projection(CASE_ID)["depth"]

    class FailingCorrectionAdapter:
        calls = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute_turn_analysis_correction_with_response_checkpoint(
            self, request, **kwargs
        ):
            self.calls += 1
            raise ProviderAdapterError(
                c.ProviderFailureReceipt(
                    provider=c.ProviderName.OPENAI,
                    stage=c.ProviderProcessingStage.TURN_ANALYSIS,
                    client_request_id=request.client_request_id,
                    provider_request_id=None,
                    status_code=429,
                    code=c.ProviderFailureCode.HTTP_429,
                    retryable=True,
                    validation_diagnostics=(),
                    occurred_at=NOW,
                )
            )

    adapter = FailingCorrectionAdapter()
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    worker = DurableAnalyzerWorker(orchestrator, worker_id="bounded-correction-worker")
    assert asyncio.run(worker.run_once()) is True
    assert asyncio.run(worker.run_once()) is False

    correction = next(
        item
        for item in foundation.analyzer_jobs(CASE_ID)
        if item["subject_id"].startswith("turn-correction:")
    )
    assert correction["state"] == "FAILED"
    assert correction["attempt_count"] == 1
    assert correction["last_error_code"] == "TURN_CORRECTION_PROVIDER_HTTP_429"
    assert adapter.calls == 1
    assert foundation.runway_projection(CASE_ID)["depth"] == depth_before


def test_known_turn_correction_response_resumes_exact_id_without_recreate(tmp_path):
    _, foundation = _runtime(tmp_path)
    context, correction = _seed_invalid_turn_correction(foundation)

    class ResumeOnlyAdapter:
        create_calls = 0
        resume_calls = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute_turn_analysis_correction_with_response_checkpoint(
            self, request, **kwargs
        ):
            self.create_calls += 1
            raise AssertionError("known correction Response must not be recreated")

        async def resume_stored_response(
            self, request, *, context, response_id
        ):
            self.resume_calls += 1
            assert response_id == "resp_known_turn_correction"
            return c.TurnAnalysisCandidate(
                protocol_version="1.0.0",
                output_type="TURN_ANALYSIS_CANDIDATE",
                analyzer_run_id=request.analyzer_run_id,
                context_id=request.context_id,
                request_hash=request.request_hash,
                source_set_hash=request.source_set_hash,
                transcript_event_id=request.transcript.transcript_event_id,
                based_on_case_revision=request.based_on_case_revision,
                disposition=c.TurnDisposition.NO_SEMANTIC_CHANGE,
                no_change_reason_code="OUT_OF_SCOPE",
                evidence_candidates=(),
                new_problems=(),
                new_problem_clusters=(),
                revised_problem_clusters=(),
                new_questions=(),
                revised_questions=(),
                low_risk_facts=(),
                decisions=(),
                problem_assessments=(),
                evidence_findings=(),
            )

    adapter = ResumeOnlyAdapter()
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    crashed_worker = DurableAnalyzerWorker(
        orchestrator, worker_id="crashed-correction-worker"
    )
    claimed = foundation.claim_analyzer_job(
        CASE_ID, worker_id="crashed-correction-worker"
    )
    assert claimed["job_id"] == correction["job_id"]
    request = crashed_worker._build_request(claimed, context)
    foundation.checkpoint_analyzer_job(
        correction["job_id"],
        worker_id="crashed-correction-worker",
        state="PROVIDER_REQUESTED",
        request_json=request.model_dump_json(),
    )
    foundation.checkpoint_provider_response(
        CASE_ID,
        client_request_id=request.client_request_id,
        operation=c.AnalyzerOperation.TURN_ANALYSIS.value,
        provider_response_id="resp_known_turn_correction",
    )
    table = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
    with foundation.engine.begin() as connection:
        connection.execute(
            table.update()
            .where(table.c.job_id == correction["job_id"])
            .values(lease_expires_at="2026-08-12T11:59:00Z")
        )

    restarted_worker = DurableAnalyzerWorker(
        orchestrator, worker_id="resumed-correction-worker"
    )
    assert asyncio.run(restarted_worker.run_once()) is True
    assert asyncio.run(restarted_worker.run_once()) is False
    completed = next(
        item
        for item in foundation.analyzer_jobs(CASE_ID)
        if item["job_id"] == correction["job_id"]
    )
    assert completed["state"] == "COMPLETED"
    assert completed["attempt_count"] == 2
    assert completed["candidate_json"] is not None
    assert adapter.create_calls == 0
    assert adapter.resume_calls == 1


def test_bootstrap_runway_is_foundation_admitted_only_at_exact_one_plus_three(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    receipt = _admit_brief(foundation)
    assert receipt.admitted_guidance_id is not None
    runway = foundation.runway_projection(CASE_ID)
    assert runway["depth"] == c.INITIAL_RUNWAY_DEPTH
    assert len({item["question_id"] for item in runway["questions"]}) == c.INITIAL_RUNWAY_DEPTH
    assert foundation.current_admitted_guidance(CASE_ID).guidance_id == receipt.admitted_guidance_id


def test_guidance_request_rehydrates_persisted_runway_ids_as_uuids(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    context = foundation.active_analyzer_context(CASE_ID)
    runway = foundation.runway_projection(CASE_ID)
    assert all(isinstance(item["question_id"], str) for item in runway["questions"])

    class SnapshotHashAdapter(DeterministicAdapter):
        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=SnapshotHashAdapter(),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    request = DurableAnalyzerWorker(orchestrator)._build_request(
        {"job_id": str(uuid4()), "operation": c.AnalyzerOperation.GUIDANCE.value},
        context,
    )

    assert isinstance(request, c.ReplenishGuidanceRequest)
    assert all(
        isinstance(item.foundation_id, UUID)
        for item in request.runway_state.active_question_refs
    )
    assert {item.foundation_id for item in request.runway_state.active_question_refs} == {
        UUID(item["question_id"]) for item in runway["questions"]
    }


def test_unsafe_bootstrap_runway_fails_closed_without_guidance(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    receipt = _admit_brief(foundation, unsafe_question_index=c.INITIAL_RUNWAY_DEPTH)
    assert receipt.admitted_guidance_id is None
    assert foundation.runway_projection(CASE_ID)["depth"] == 0


def test_guidance_is_selection_only_and_rejects_question_text_smuggling(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    admitted = foundation.current_admitted_guidance(CASE_ID)
    assert admitted is not None
    assert "new_questions" not in str(c.GuidanceCandidate.model_json_schema())
    run_id = uuid4()
    request_hash = "sha256:" + "8" * 64
    candidate = c.GuidanceCandidate(
        protocol_version="1.0.0",
        output_type="GUIDANCE_CANDIDATE",
        analyzer_run_id=run_id,
        context_id=CONTEXT_ID,
        request_hash=request_hash,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=foundation.case_revision(CASE_ID),
        recommended_question=c.GuidanceQuestion(
            question_ref=c.FoundationEntityRef(
                ref_kind="FOUNDATION_ID",
                foundation_id=admitted.recommended_question.question_id,
                expected_version=admitted.recommended_question.question_version,
            ),
            exact_text="This invented question was never admitted.",
            reason="Attempted question smuggling.",
        ),
        safe_alternates=(),
        do_not_ask_question_refs=(),
        dependencies=(
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                entity_ref=None,
            ),
        ),
        acknowledgement_suggestion="A safe question is available.",
    )
    values = _base(foundation.case_revision(CASE_ID))
    values.update(
        command_type="ADMIT_GUIDANCE",
        analyzer_run_id=run_id,
        context_id=CONTEXT_ID,
        provider_request_hash=request_hash,
        candidate=candidate,
    )
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(c.AdmitGuidanceCommand(**values))
    assert error.value.code is c.FoundationRejectionCode.STALE_ENTITY


def test_foundation_derives_current_dependencies_for_every_guidance_question(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    current = foundation.current_admitted_guidance(CASE_ID)
    selected = (current.recommended_question, *current.safe_alternates)
    run_id = uuid4()
    request_hash = "sha256:" + "7" * 64
    candidate = c.GuidanceCandidate(
        protocol_version="1.0.0",
        output_type="GUIDANCE_CANDIDATE",
        analyzer_run_id=run_id,
        context_id=CONTEXT_ID,
        request_hash=request_hash,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=foundation.case_revision(CASE_ID),
        recommended_question=c.GuidanceQuestion(
            question_ref=c.FoundationEntityRef(
                ref_kind="FOUNDATION_ID",
                foundation_id=selected[0].question_id,
                expected_version=selected[0].question_version,
            ),
            exact_text=selected[0].exact_text,
            reason=selected[0].reason,
        ),
        safe_alternates=tuple(
            c.GuidanceQuestion(
                question_ref=c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=item.question_id,
                    expected_version=item.question_version,
                ),
                exact_text=item.exact_text,
                reason=item.reason,
            )
            for item in selected[1:]
        ),
        do_not_ask_question_refs=(),
        # Terra need not be trusted to enumerate the selected questions as
        # dependencies; Foundation derives and validates them on admission.
        dependencies=(
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                entity_ref=None,
            ),
        ),
        acknowledgement_suggestion="The next admitted clarification is ready.",
    )
    values = _base(foundation.case_revision(CASE_ID))
    values.update(
        command_type="ADMIT_GUIDANCE",
        analyzer_run_id=run_id,
        context_id=CONTEXT_ID,
        provider_request_hash=request_hash,
        candidate=candidate,
    )
    foundation.execute(c.AdmitGuidanceCommand(**values))
    admitted = foundation.current_admitted_guidance(CASE_ID)
    question_dependencies = {
        (item.entity_id, item.expected_version)
        for item in admitted.dependencies
        if item.dependency_kind is c.GuidanceDependencyKind.QUESTION
    }
    assert question_dependencies == {
        (item.question_id, item.question_version) for item in selected
    }
    assert any(
        item.dependency_kind is c.GuidanceDependencyKind.PROBLEM
        for item in admitted.dependencies
    )


def test_transcript_and_pending_analysis_job_are_atomic_and_replay_deduplicates(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    command_value = _transcript_command(4, 1, "The first answer is final.")
    receipt = foundation.execute(command_value)
    replay = foundation.execute(command_value)
    assert replay == receipt
    jobs = foundation.analyzer_jobs(CASE_ID)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "ANALYSIS_PENDING"
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"])
        ).scalar_one() == 1
        assert connection.execute(
            select(func.count()).select_from(V0_RUNTIME_TABLES["workshop_analyzer_jobs"])
        ).scalar_one() == 1


def test_transcript_rolls_back_when_atomic_job_insert_fails(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    command_value = _transcript_command(4, 1, "This transaction must roll back.")
    jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
    event_id = command_value.transcript.event_id
    now = NOW.isoformat().replace("+00:00", "Z")
    with foundation.engine.begin() as connection:
        connection.execute(
            jobs.insert().values(
                job_id=str(uuid4()),
                case_id=str(CASE_ID),
                session_id=str(command_value.session_id),
                operation="TURN_ANALYSIS",
                subject_id="preexisting-conflict",
                dedupe_key=f"turn-analysis:{CASE_ID}:{event_id}",
                priority=10,
                state="ANALYSIS_PENDING",
                provider_request_id="preexisting-conflict",
                request_json=None,
                candidate_json=None,
                admission_receipt_json=None,
                attempt_count=0,
                lease_owner=None,
                lease_expires_at=None,
                available_at=now,
                last_error_code=None,
                created_at=now,
                updated_at=now,
            )
        )
    with pytest.raises(IntegrityError):
        foundation.execute(command_value)
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count())
            .select_from(WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"])
            .where(
                WORKSHOP_PROTOCOL_TABLES["workshop_final_transcripts"].c.event_id
                == str(event_id)
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count())
            .select_from(WORKSHOP_PROTOCOL_TABLES["workshop_command_ledger"])
            .where(
                WORKSHOP_PROTOCOL_TABLES["workshop_command_ledger"].c.idempotency_key
                == command_value.idempotency_key
            )
        ).scalar_one() == 0


def test_depth_two_enqueues_one_guidance_and_turn_jobs_remain_first(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    for sequence in range(1, 3):
        foundation.execute(
            _transcript_command(
                foundation.case_revision(CASE_ID), sequence, f"Final answer {sequence}."
            )
        )
    assert foundation.runway_projection(CASE_ID)["depth"] == 2
    jobs = foundation.analyzer_jobs(CASE_ID)
    assert sum(item["operation"] == "GUIDANCE" for item in jobs) == 1
    claimed = foundation.claim_analyzer_job(CASE_ID, worker_id="test-worker")
    assert claimed["operation"] == "TURN_ANALYSIS"


def test_zero_runway_instruction_is_fixed_and_never_invents_a_question(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    for sequence in range(1, c.INITIAL_RUNWAY_DEPTH + 1):
        foundation.execute(
            _transcript_command(
                foundation.case_revision(CASE_ID), sequence, f"Final answer {sequence}."
            )
        )
    runway = foundation.runway_projection(CASE_ID)
    card = foundation.voice_session_card(CASE_ID)
    instruction = V4LiveTransport._voice_instruction(card, runway)
    assert runway["depth"] == 0
    assert card.runway_health is c.RunwayHealth.SAFE_RECOVERY_ONLY
    assert "No substantive clarification question is currently admitted" in instruction
    assert "preparing the next clarification area" in instruction


def test_endangered_runway_blocks_spec_synthesis(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    for sequence in range(1, 3):
        foundation.execute(
            _transcript_command(
                foundation.case_revision(CASE_ID), sequence, f"Final answer {sequence}."
            )
        )
    context = foundation.active_analyzer_context(CASE_ID)
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=SimpleNamespace(source_set_hash=lambda sources: SOURCE_SET_HASH),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    with pytest.raises(ValueError, match="runway is endangered"):
        asyncio.run(orchestrator.synthesize_artifact("SPEC_PACKAGE", operation_key="blocked"))


def test_voice_provider_is_not_created_before_ready():
    async def scenario():
        calls = 0

        class Provider:
            async def connect(self, context):
                nonlocal calls
                calls += 1

        class Socket:
            def __init__(self):
                self.messages = []
                self.closed = None

            async def accept(self):
                return None

            async def send_json(self, value):
                self.messages.append(value)

            async def close(self, code):
                self.closed = code

        foundation = SimpleNamespace(
            preparation_projection=lambda case_id: {"phase": "VALIDATING_DOCUMENTS"},
            runway_projection=lambda case_id: {"depth": 0},
        )
        orchestrator = SimpleNamespace(foundation=foundation, case_id=uuid4())
        socket = Socket()
        await V4LiveTransport(Provider(), orchestrator).handle(socket)
        assert calls == 0
        assert socket.closed == 4403
        assert socket.messages == [
            {"type": "ERROR", "code": "WORKSHOP_PREPARATION_NOT_READY"}
        ]

    asyncio.run(scenario())


def test_voice_provider_close_timeout_cancels_teardown_without_blocking_transport(caplog):
    async def scenario():
        class HangingSession:
            def __init__(self):
                self.cancelled = False

            async def close(self):
                try:
                    await asyncio.Event().wait()
                finally:
                    self.cancelled = True

        session = HangingSession()
        transport = V4LiveTransport(
            SimpleNamespace(),
            SimpleNamespace(case_id=CASE_ID),
            provider_close_timeout_seconds=0.01,
        )
        await asyncio.wait_for(transport._close_provider_session(session), timeout=0.2)
        await asyncio.sleep(0)
        assert session.cancelled is True

    with caplog.at_level("INFO", logger="specops.workshop.voice"):
        asyncio.run(scenario())
    events = [json.loads(record.message) for record in caplog.records]
    assert [event["event"] for event in events] == [
        "voice_provider_close.started",
        "voice_provider_close.timed_out",
    ]
    assert all(event["correlation_id"] == str(CASE_ID) for event in events)
    assert all(event["duration_ms"] >= 0 for event in events)


def test_voice_guidance_refresh_is_applied_only_at_final_turn_boundary():
    async def scenario():
        events = []

        class Socket:
            async def send_json(self, value):
                events.append(("ack", value))

        class Session:
            async def send_text(self, value):
                events.append(("guidance", value))

        foundation = SimpleNamespace(
            case_actor=lambda case_id, role: uuid4(),
            voice_session_card=lambda case_id: SimpleNamespace(
                runway_health=SimpleNamespace(value="HEALTHY")
            ),
            runway_projection=lambda case_id: {
                "depth": 1,
                "questions": [
                    {
                        "exact_text": "Which Foundation-admitted export choice applies?"
                    }
                ],
            },
        )

        async def record(value):
            events.append(("commit", value.provider_request_id))
            return SimpleNamespace(
                transcript=SimpleNamespace(
                    transcript_event_id=uuid4(),
                    command=SimpleNamespace(resulting_case_revision=9)
                ),
                duplicate=False,
            )

        orchestrator = SimpleNamespace(
            case_id=uuid4(),
            foundation=foundation,
            record_final_transcript=record,
            complete_from_final_transcript=lambda event_id: asyncio.sleep(
                0, result=SimpleNamespace(action="NONE", completion=None)
            ),
        )
        transport = V4LiveTransport(SimpleNamespace(), orchestrator)
        await transport._commit(
            Socket(),
            {"turn_sequence": 1, "text": "Final answer."},
            provider_id="turn-boundary-one",
            session=Session(),
        )
        assert [kind for kind, _ in events] == ["commit", "ack", "guidance"]
        assert "Foundation-admitted questions" in events[-1][1]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("text_value", "expected"),
    (
        ("I complete the Spec Workshop.", CompletionUtterance.EXPLICIT),
        ("I have finished the spec workshop.", CompletionUtterance.EXPLICIT),
        ("I have defined everything that I need to.", CompletionUtterance.EXPLICIT),
        ("I think we're done.", CompletionUtterance.AMBIGUOUS),
        ("That should be all.", CompletionUtterance.AMBIGUOUS),
        ("Yes, I confirm.", CompletionUtterance.AFFIRMATIVE),
        ("I haven't finished the Spec Workshop.", CompletionUtterance.NONE),
        ("Continue with the next question.", CompletionUtterance.NONE),
    ),
)
def test_completion_language_is_deterministic_and_negation_safe(text_value, expected):
    assert classify_completion_utterance(text_value) is expected


def test_ambiguous_voice_completion_asks_exact_confirmation_before_guidance():
    async def scenario():
        events = []
        transcript_id = uuid4()

        class Socket:
            async def send_json(self, value):
                events.append(("socket", value))

        class Session:
            async def send_text(self, value):
                events.append(("provider", value))

        foundation = SimpleNamespace(case_actor=lambda case_id, role: uuid4())

        async def record(value):
            return SimpleNamespace(
                transcript=SimpleNamespace(
                    transcript_event_id=transcript_id,
                    command=SimpleNamespace(resulting_case_revision=9),
                ),
                duplicate=False,
            )

        async def completion(event_id):
            assert event_id == transcript_id
            return SimpleNamespace(action="CONFIRMATION_REQUIRED", completion=None)

        orchestrator = SimpleNamespace(
            case_id=uuid4(),
            foundation=foundation,
            record_final_transcript=record,
            complete_from_final_transcript=completion,
        )
        completed = await V4LiveTransport(SimpleNamespace(), orchestrator)._commit(
            Socket(),
            {"turn_sequence": 1, "text": "I think we're done."},
            provider_id="ambiguous-finish",
            session=Session(),
        )
        assert completed is False
        assert events[1] == (
            "socket",
            {"type": "COMPLETION_CONFIRMATION_REQUIRED"},
        )
        assert "Would you like me to finish the Spec Workshop now?" in events[2][1]
        assert "Do not ask a substantive question" in events[2][1]

    asyncio.run(scenario())


def test_delayed_projection_uses_server_time_and_exact_copy(tmp_path):
    _, foundation = _runtime(tmp_path)
    table = V0_RUNTIME_TABLES["workshop_preparations"]
    with foundation.engine.begin() as connection:
        connection.execute(
            table.update()
            .where(table.c.case_id == str(CASE_ID))
            .values(started_at="2026-08-12T11:59:29Z")
        )
    projection = foundation.preparation_projection(CASE_ID)
    assert projection["delayed"] is True
    assert projection["delayed_message"] == (
        "SpecOps Analyzer is taking a little longer to formulate your Workshop plan. "
        "Your documents are safe, and preparation is continuing."
    )
    foundation.set_preparation_phase(CASE_ID, "FAILED", failure_code="SAFE_FAILURE")
    failed = foundation.preparation_projection(CASE_ID)
    assert failed["delayed"] is False
    assert failed["delayed_message"] is None


class CancellablePreparationAdapter(DeterministicAdapter):
    """Deterministic remote: repeat attempts reuse one logical resource/result."""

    def __init__(self, cancel_stage: str):
        super().__init__(conversation_id="conv_task27_cancellable")
        self.cancel_stage = cancel_stage
        self.cancelled = False
        self.uploaded: dict[str, str] = {}
        self.upload_creations = 0
        self.conversation_creations = 0
        self.bootstrap_creations = 0
        self.bootstrap_result: BootstrapResult | None = None
        self.cleanup_attempts = 0
        self.cleaned_resources: set[str] = set()

    def _cancel_once(self, stage: str) -> None:
        if self.cancel_stage == stage and not self.cancelled:
            self.cancelled = True
            raise asyncio.CancelledError

    async def upload_source(self, item):
        key = str(item.source.source_id)
        if key not in self.uploaded:
            self.upload_creations += 1
            self.uploaded[key] = f"file_task27_{self.upload_creations}"
        self._cancel_once(f"upload:{item.source.role.value}")
        return self.uploaded[key]

    async def create_conversation(self, sources):
        if self.conversation_creations == 0:
            self.conversation_creations = 1
        self._cancel_once("conversation")
        return self.conversation_id

    def prepared_from_ids(self, sources, file_ids, conversation_id):
        return PreparedProviderContext(
            provider_conversation_id=conversation_id,
            source_set=c.SourceSetBinding(
                source_set_hash=self.source_set_hash(tuple(item.source for item in sources)),
                ordered_sources=tuple(
                    c.ProviderSourceBinding(source=item.source, provider_file_id=file_id)
                    for item, file_id in zip(sources, file_ids, strict=True)
                ),
            ),
        )

    async def bootstrap(self, request, *, prepared, session_id):
        if self.bootstrap_result is None:
            self.bootstrap_creations += 1
            self.bootstrap_result = await super().bootstrap(
                request, prepared=prepared, session_id=session_id
            )
        self._cancel_once("bootstrap")
        return self.bootstrap_result

    async def release_resource_ids(
        self,
        conversation_id,
        file_ids,
        *,
        response_id=None,
        response_ids=(),
    ):
        self.cleanup_attempts += 1
        self.cleaned_resources.update(
            identity
            for identity in (response_id, *response_ids, conversation_id, *file_ids)
            if identity is not None
        )
        self._cancel_once("cleanup")
        return ProviderCleanupReceipt(
            deletions=tuple(
                ProviderResourceDeletion(
                    resource_kind=(
                        "RESPONSE"
                        if identity == response_id or identity in response_ids
                        else "CONVERSATION"
                        if identity == conversation_id
                        else "FILE"
                    ),
                    resource_id=identity,
                    client_request_id=f"delete-{identity}",
                    provider_request_id=None,
                    outcome="DELETED",
                    safe_error_code=None,
                    retryable=False,
                    duration_ms=1,
                )
                for identity in (response_id, *response_ids, conversation_id, *file_ids)
                if identity is not None
            )
        )


class SplitBootstrapCancellationAdapter(CancellablePreparationAdapter):
    def __init__(self):
        super().__init__("never")
        self.start_calls = 0
        self.finish_calls = 0

    async def start_bootstrap(self, request, *, prepared):
        self.start_calls += 1
        return "resp_task27_durable_background"

    async def finish_bootstrap(
        self, request, *, prepared, session_id, response_id
    ):
        self.finish_calls += 1
        assert response_id == "resp_task27_durable_background"
        if self.finish_calls == 1:
            raise asyncio.CancelledError
        return await DeterministicAdapter.bootstrap(
            self, request, prepared=prepared, session_id=session_id
        )


def test_bootstrap_response_identity_is_durable_before_cancelled_wait_resumes(tmp_path):
    adapter = SplitBootstrapCancellationAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrator.prepare_workshop())
    resources = app.state.workshop_protocol_foundation.preparation_resources(
        app.state.bootstrap.case_id
    )
    assert resources["bootstrap_response_id"] == "resp_task27_durable_background"

    projection = asyncio.run(orchestrator.prepare_workshop())
    assert projection["phase"] == "READY"
    assert adapter.start_calls == 1
    assert adapter.finish_calls == 2


def test_cancelled_bootstrap_create_without_response_id_fails_closed_without_retry(tmp_path):
    class UncertainStartAdapter(CancellablePreparationAdapter):
        start_calls = 0

        def __init__(self):
            super().__init__("never")

        async def start_bootstrap(self, request, *, prepared):
            self.start_calls += 1
            raise asyncio.CancelledError

        async def finish_bootstrap(self, request, *, prepared, session_id, response_id):
            raise AssertionError("an unknown Response identity cannot be polled")

    adapter = UncertainStartAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrator.prepare_workshop())
    failed = app.state.workshop_protocol_foundation.preparation_projection(
        app.state.bootstrap.case_id
    )
    assert failed["phase"] == "FAILED"
    assert failed["failure_code"] == "BOOTSTRAP_RESPONSE_ID_UNCERTAIN"
    assert failed["cleanup_state"] == "RETAIN_UNCERTAIN"

    assert asyncio.run(orchestrator.prepare_workshop())["phase"] == "FAILED"
    assert adapter.start_calls == 1


def test_rejected_bootstrap_terminalizes_context_and_enters_cleanup_without_retry(tmp_path):
    class RejectedBootstrapAdapter(CancellablePreparationAdapter):
        async def bootstrap(self, request, *, prepared, session_id):
            result = await super().bootstrap(
                request, prepared=prepared, session_id=session_id
            )
            evidence = result.candidate.evidence_candidates[0].model_copy(
                update={
                    "locator": c.QuoteSearchLocator(
                        locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
                        exact_quote="This exact quote is not present in the PM source.",
                        occurrence=1,
                    ),
                    "quoted_text_candidate": (
                        "This exact quote is not present in the PM source."
                    ),
                }
            )
            candidate = result.candidate.model_copy(
                update={"evidence_candidates": (evidence,)}
            )
            return BootstrapResult(context=result.context, candidate=candidate)

    adapter = RejectedBootstrapAdapter("never")
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id

    failed = asyncio.run(orchestrator.prepare_workshop())
    assert failed["phase"] == "FAILED"
    assert failed["failure_code"] == "FOUNDATION_REJECTED_EVIDENCE_BINDING_FAILED"
    assert failed["cleanup_state"] == "PENDING"
    assert failed["cleanup_reason"] == "UNRECOVERABLE_PREPARATION_FAILURE"
    assert foundation.active_analyzer_context(case_id) is None
    assert adapter.bootstrap_creations == 1

    assert asyncio.run(orchestrator.prepare_workshop()) == failed
    assert adapter.bootstrap_creations == 1

    cleaned = asyncio.run(orchestrator.cleanup_preparation())
    assert cleaned["cleanup_state"] == "COMPLETED"
    assert foundation.preparation_resources(case_id)["bootstrap_response_id"] is None
    assert adapter.cleaned_resources == {
        "resp_bootstrap",
        "conv_task27_cancellable",
        "file_task27_1",
        "file_task27_2",
    }


def test_completed_unbound_bootstrap_gets_one_durable_grounding_correction(tmp_path):
    class CorrectingBootstrapAdapter(CancellablePreparationAdapter):
        def __init__(self):
            super().__init__("never")
            self.correction_start_calls = 0
            self.correction_finish_calls = 0
            self.cancel_known_response_once = True
            self.initial_client_request_id = None
            self.correction_client_request_id = None
            self.correction_finish_client_request_ids = []

        async def bootstrap(self, request, *, prepared, session_id):
            self.initial_client_request_id = request.client_request_id
            result = await super().bootstrap(
                request, prepared=prepared, session_id=session_id
            )
            evidence = result.candidate.evidence_candidates[0].model_copy(
                update={
                    "locator": c.QuoteSearchLocator(
                        locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
                        exact_quote="This exact quote is absent from the PM source.",
                        occurrence=1,
                    ),
                    "quoted_text_candidate": (
                        "This exact quote is absent from the PM source."
                    ),
                }
            )
            return BootstrapResult(
                context=result.context,
                candidate=result.candidate.model_copy(
                    update={"evidence_candidates": (evidence,)}
                ),
            )

        async def start_bootstrap_grounding_correction(self, request, *, prepared):
            self.correction_start_calls += 1
            self.correction_client_request_id = request.client_request_id
            return "resp_bootstrap_grounding_correction"

        async def finish_bootstrap(
            self, request, *, prepared, session_id, response_id
        ):
            self.correction_finish_calls += 1
            self.correction_finish_client_request_ids.append(request.client_request_id)
            assert response_id == "resp_bootstrap_grounding_correction"
            if self.cancel_known_response_once:
                self.cancel_known_response_once = False
                raise asyncio.CancelledError
            result = await DeterministicAdapter.bootstrap(
                self, request, prepared=prepared, session_id=session_id
            )
            return BootstrapResult(
                context=result.context.model_copy(
                    update={"bootstrap_response_id": response_id}
                ),
                candidate=result.candidate,
            )

    adapter = CorrectingBootstrapAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrator.prepare_workshop())
    pending = foundation.pending_provider_responses(case_id)
    assert len(pending) == 1
    assert pending[0]["operation"] == "BOOTSTRAP_GROUNDING_CORRECTION"
    assert pending[0]["provider_response_id"] == "resp_bootstrap_grounding_correction"

    ready = asyncio.run(orchestrator.prepare_workshop())
    assert ready["phase"] == "READY"
    assert adapter.bootstrap_creations == 1
    assert adapter.correction_start_calls == 1
    assert adapter.correction_finish_calls == 2
    assert adapter.correction_client_request_id != adapter.initial_client_request_id
    assert adapter.correction_finish_client_request_ids == [
        adapter.correction_client_request_id,
        adapter.correction_client_request_id,
    ]
    resources = foundation.preparation_resources(case_id)
    assert resources["bootstrap_response_id"] == "resp_bootstrap"
    assert (
        foundation.active_analyzer_context(case_id).bootstrap_response_id
        == "resp_bootstrap_grounding_correction"
    )


def test_unknown_question_problem_reference_gets_one_durable_graph_correction(tmp_path):
    class GraphCorrectingAdapter(CancellablePreparationAdapter):
        def __init__(self):
            super().__init__("never")
            self.primary_start_calls = 0
            self.primary_finish_calls = 0
            self.graph_start_calls = 0
            self.graph_finish_calls = 0
            self.primary_client_request_id = None
            self.correction_client_request_id = None

        async def start_bootstrap(self, request, *, prepared):
            self.primary_start_calls += 1
            self.primary_client_request_id = request.client_request_id
            return "resp_bootstrap_graph_invalid"

        async def start_bootstrap_graph_correction(self, request, *, prepared):
            self.graph_start_calls += 1
            self.correction_client_request_id = request.client_request_id
            return "resp_bootstrap_graph_correction"

        async def start_bootstrap_grounding_correction(self, request, *, prepared):
            raise AssertionError("the shared correction slot cannot run twice")

        async def finish_bootstrap(
            self, request, *, prepared, session_id, response_id
        ):
            if response_id == "resp_bootstrap_graph_invalid":
                self.primary_finish_calls += 1
                raise _bootstrap_provider_error(
                    c.ProviderFailureCode.OUTPUT_INVALID,
                    correction_code="UNKNOWN_QUESTION_PROBLEM_REFERENCE",
                )
            assert response_id == "resp_bootstrap_graph_correction"
            self.graph_finish_calls += 1
            result = await DeterministicAdapter.bootstrap(
                self, request, prepared=prepared, session_id=session_id
            )
            return BootstrapResult(
                context=result.context.model_copy(
                    update={"bootstrap_response_id": response_id}
                ),
                candidate=result.candidate,
            )

    adapter = GraphCorrectingAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id

    ready = asyncio.run(orchestrator.prepare_workshop())

    assert ready["phase"] == "READY"
    assert adapter.primary_start_calls == 1
    assert adapter.primary_finish_calls == 1
    assert adapter.graph_start_calls == 1
    assert adapter.graph_finish_calls == 1
    assert adapter.correction_client_request_id != adapter.primary_client_request_id
    resources = foundation.preparation_resources(case_id)
    assert resources["bootstrap_response_id"] == "resp_bootstrap_graph_invalid"
    pending = foundation.pending_provider_responses(case_id)
    assert len(pending) == 1
    assert pending[0]["operation"] == "BOOTSTRAP_GROUNDING_CORRECTION"
    assert pending[0]["provider_response_id"] == "resp_bootstrap_graph_correction"
    assert (
        foundation.active_analyzer_context(case_id).bootstrap_response_id
        == "resp_bootstrap_graph_correction"
    )


def test_bad_graph_correction_terminalizes_without_a_third_generation(tmp_path):
    class BadGraphCorrectionAdapter(CancellablePreparationAdapter):
        def __init__(self):
            super().__init__("never")
            self.primary_start_calls = 0
            self.graph_start_calls = 0
            self.finish_calls = 0

        async def start_bootstrap(self, request, *, prepared):
            self.primary_start_calls += 1
            return "resp_bootstrap_graph_invalid"

        async def start_bootstrap_graph_correction(self, request, *, prepared):
            self.graph_start_calls += 1
            return "resp_bootstrap_graph_still_invalid"

        async def start_bootstrap_grounding_correction(self, request, *, prepared):
            raise AssertionError("a second correction must never be created")

        async def finish_bootstrap(
            self, request, *, prepared, session_id, response_id
        ):
            self.finish_calls += 1
            raise _bootstrap_provider_error(
                c.ProviderFailureCode.OUTPUT_INVALID,
                correction_code="UNKNOWN_QUESTION_PROBLEM_REFERENCE",
            )

    adapter = BadGraphCorrectionAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id

    failed = asyncio.run(orchestrator.prepare_workshop())

    assert failed["phase"] == "FAILED"
    assert failed["failure_code"] == "BOOTSTRAP_CORRECTION_OUTPUT_INVALID"
    assert failed["cleanup_state"] == "PENDING"
    assert failed["cleanup_reason"] == "UNRECOVERABLE_PREPARATION_FAILURE"
    assert adapter.primary_start_calls == 1
    assert adapter.graph_start_calls == 1
    assert adapter.finish_calls == 2
    assert asyncio.run(orchestrator.prepare_workshop()) == failed
    assert adapter.graph_start_calls == 1

    cleaned = asyncio.run(orchestrator.cleanup_preparation())
    assert cleaned["cleanup_state"] == "COMPLETED"
    assert adapter.cleaned_resources == {
        "resp_bootstrap_graph_invalid",
        "resp_bootstrap_graph_still_invalid",
        "conv_task27_cancellable",
        "file_task27_1",
        "file_task27_2",
    }


def test_known_bootstrap_response_transport_failure_resumes_without_recreate(tmp_path):
    class ResumableBootstrapAdapter(CancellablePreparationAdapter):
        def __init__(self):
            super().__init__("never")
            self.start_calls = 0
            self.finish_calls = 0

        async def start_bootstrap(self, request, *, prepared):
            self.start_calls += 1
            return "resp_bootstrap_known"

        async def finish_bootstrap(
            self, request, *, prepared, session_id, response_id
        ):
            self.finish_calls += 1
            assert response_id == "resp_bootstrap_known"
            if self.finish_calls == 1:
                raise _bootstrap_provider_error(c.ProviderFailureCode.TIMEOUT)
            result = await DeterministicAdapter.bootstrap(
                self, request, prepared=prepared, session_id=session_id
            )
            return BootstrapResult(
                context=result.context.model_copy(
                    update={"bootstrap_response_id": response_id}
                ),
                candidate=result.candidate,
            )

    adapter = ResumableBootstrapAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator

    with pytest.raises(ProviderAdapterError) as captured:
        asyncio.run(orchestrator.prepare_workshop())
    assert captured.value.receipt.code is c.ProviderFailureCode.TIMEOUT

    ready = asyncio.run(orchestrator.prepare_workshop())
    assert ready["phase"] == "READY"
    assert adapter.start_calls == 1
    assert adapter.finish_calls == 2


def test_uncertain_grounding_correction_create_is_never_repeated(tmp_path):
    class UncertainCorrectionAdapter(CancellablePreparationAdapter):
        def __init__(self):
            super().__init__("never")
            self.correction_start_calls = 0

        async def bootstrap(self, request, *, prepared, session_id):
            result = await super().bootstrap(
                request, prepared=prepared, session_id=session_id
            )
            evidence = result.candidate.evidence_candidates[0].model_copy(
                update={
                    "locator": c.QuoteSearchLocator(
                        locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
                        exact_quote="This exact quote is absent from the PM source.",
                        occurrence=1,
                    ),
                    "quoted_text_candidate": (
                        "This exact quote is absent from the PM source."
                    ),
                }
            )
            return BootstrapResult(
                context=result.context,
                candidate=result.candidate.model_copy(
                    update={"evidence_candidates": (evidence,)}
                ),
            )

        async def start_bootstrap_grounding_correction(self, request, *, prepared):
            self.correction_start_calls += 1
            raise asyncio.CancelledError

        async def finish_bootstrap(
            self, request, *, prepared, session_id, response_id
        ):
            raise AssertionError("an unknown correction Response cannot be polled")

    adapter = UncertainCorrectionAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrator.prepare_workshop())
    failed = foundation.preparation_projection(case_id)
    assert failed["phase"] == "FAILED"
    assert failed["failure_code"] == "BOOTSTRAP_CORRECTION_RESPONSE_ID_UNCERTAIN"
    assert failed["cleanup_state"] == "RETAIN_UNCERTAIN"
    assert foundation.pending_provider_responses(case_id) == ()

    assert asyncio.run(orchestrator.prepare_workshop()) == failed
    assert adapter.bootstrap_creations == 1
    assert adapter.correction_start_calls == 1


def test_bad_grounding_correction_is_foundation_rejected_once_and_fully_cleaned(tmp_path):
    class BadCorrectionAdapter(CancellablePreparationAdapter):
        def __init__(self):
            super().__init__("never")
            self.correction_start_calls = 0
            self.correction_finish_calls = 0

        @staticmethod
        def _unbound(result, response_id):
            evidence = result.candidate.evidence_candidates[0].model_copy(
                update={
                    "locator": c.QuoteSearchLocator(
                        locator_kind=c.SourceLocatorKind.QUOTE_SEARCH,
                        exact_quote="This exact quote is absent from the PM source.",
                        occurrence=1,
                    ),
                    "quoted_text_candidate": (
                        "This exact quote is absent from the PM source."
                    ),
                }
            )
            return BootstrapResult(
                context=result.context.model_copy(
                    update={"bootstrap_response_id": response_id}
                ),
                candidate=result.candidate.model_copy(
                    update={"evidence_candidates": (evidence,)}
                ),
            )

        async def bootstrap(self, request, *, prepared, session_id):
            result = await super().bootstrap(
                request, prepared=prepared, session_id=session_id
            )
            return self._unbound(result, "resp_bootstrap")

        async def start_bootstrap_grounding_correction(self, request, *, prepared):
            self.correction_start_calls += 1
            return "resp_bad_grounding_correction"

        async def finish_bootstrap(
            self, request, *, prepared, session_id, response_id
        ):
            self.correction_finish_calls += 1
            result = await DeterministicAdapter.bootstrap(
                self, request, prepared=prepared, session_id=session_id
            )
            return self._unbound(result, response_id)

    adapter = BadCorrectionAdapter()
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id

    failed = asyncio.run(orchestrator.prepare_workshop())
    assert failed["phase"] == "FAILED"
    assert failed["failure_code"] == "FOUNDATION_REJECTED_EVIDENCE_BINDING_FAILED"
    assert failed["cleanup_state"] == "PENDING"
    assert foundation.active_analyzer_context(case_id) is None
    assert adapter.bootstrap_creations == 1
    assert adapter.correction_start_calls == 1
    assert adapter.correction_finish_calls == 1

    assert asyncio.run(orchestrator.prepare_workshop()) == failed
    assert adapter.correction_start_calls == 1
    cleaned = asyncio.run(orchestrator.cleanup_preparation())
    assert cleaned["cleanup_state"] == "COMPLETED"
    assert adapter.cleaned_resources == {
        "resp_bootstrap",
        "resp_bad_grounding_correction",
        "conv_task27_cancellable",
        "file_task27_1",
        "file_task27_2",
    }


@pytest.mark.parametrize(
    "stage",
    (
        "upload:PM_SPEC",
        "upload:TECHNICAL_CONTRACT",
        "conversation",
        "bootstrap",
    ),
)
def test_real_preparation_cancellation_resumes_without_duplicate_logical_work(
    tmp_path, stage
):
    adapter = CancellablePreparationAdapter(stage)
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrator.prepare_workshop())
    projection = asyncio.run(orchestrator.prepare_workshop())
    resources = app.state.workshop_protocol_foundation.preparation_resources(
        app.state.bootstrap.case_id
    )
    assert projection["phase"] == "READY"
    assert resources["pm_file_id"] is not None
    assert resources["technical_file_id"] is not None
    assert resources["provider_conversation_id"] == adapter.conversation_id
    assert adapter.upload_creations == 2
    assert adapter.conversation_creations == 1
    assert adapter.bootstrap_creations == 1


def test_real_cleanup_cancellation_stays_resumable_and_idempotent(tmp_path):
    adapter = CancellablePreparationAdapter("cleanup")
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    foundation = app.state.workshop_protocol_foundation
    case_id = app.state.bootstrap.case_id
    foundation.checkpoint_preparation_resource(
        case_id,
        pm_file_id="file_pm",
        technical_file_id="file_technical",
        provider_conversation_id="conversation_cleanup",
    )
    foundation.set_preparation_phase(
        case_id,
        "FAILED",
        failure_code="PREPARATION_CANCELLED",
        cleanup_state="PENDING",
    )
    orchestrator = app.state.workshop_protocol_orchestrator
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orchestrator.cleanup_preparation())
    assert foundation.preparation_projection(case_id)["cleanup_state"] == "IN_PROGRESS"
    completed = asyncio.run(orchestrator.cleanup_preparation())
    assert completed["cleanup_state"] == "COMPLETED"
    resources = foundation.preparation_resources(case_id)
    assert resources["pm_file_id"] is None
    assert resources["technical_file_id"] is None
    assert resources["provider_conversation_id"] is None
    assert adapter.cleanup_attempts == 2
    assert adapter.cleaned_resources == {
        "file_pm",
        "file_technical",
        "conversation_cleanup",
    }


def test_last_browser_client_gets_exact_fifteen_minute_resume_grace(tmp_path):
    class MutableClock:
        value = NOW

        def now(self):
            return self.value

        def advance(self, seconds: int):
            self.value += timedelta(seconds=seconds)

    async def scenario():
        clock = MutableClock()
        adapter = CancellablePreparationAdapter("never")
        app = create_app(
            settings=configured(tmp_path),
            clock=clock,
            source_catalog=SourceCatalog(ROOT),
            live_provider=object(),
            analyzer_adapter=adapter,
        )
        orchestrator = app.state.workshop_protocol_orchestrator
        foundation = app.state.workshop_protocol_foundation
        await orchestrator.prepare_workshop()
        original = foundation.active_analyzer_context(app.state.bootstrap.case_id)

        assert await orchestrator.note_last_client_disconnected() == "RESTART_GRACE"
        grace = foundation.preparation_projection(app.state.bootstrap.case_id)
        assert grace["last_client_disconnected_at"] == "2026-08-12T12:00:00Z"
        assert grace["restart_grace_until"] == "2026-08-12T12:15:00Z"

        clock.advance(899)
        assert await orchestrator.note_client_connected() == "RESUMED_WITHIN_GRACE"
        assert foundation.active_analyzer_context(app.state.bootstrap.case_id) == original
        assert foundation.preparation_projection(app.state.bootstrap.case_id)[
            "cleanup_state"
        ] == "NOT_REQUIRED"

        assert await orchestrator.note_last_client_disconnected() == "RESTART_GRACE"
        clock.advance(900)
        assert await orchestrator.expire_restart_grace() is True
        expired = foundation.preparation_projection(app.state.bootstrap.case_id)
        assert expired["cleanup_state"] == "PENDING"
        assert expired["cleanup_reason"] == "RESTART_GRACE_EXPIRED"
        assert foundation.active_analyzer_context(app.state.bootstrap.case_id) is None

        completed = await orchestrator.cleanup_preparation()
        assert completed["cleanup_state"] == "COMPLETED"
        assert await orchestrator.note_client_connected() == "REBUILD_REQUIRED"
        assert foundation.preparation_projection(app.state.bootstrap.case_id)[
            "phase"
        ] == "VALIDATING_DOCUMENTS"

    asyncio.run(scenario())


def test_presence_websocket_starts_grace_only_after_last_browser_page_closes(tmp_path):
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=DeterministicAdapter(),
    )
    asyncio.run(app.state.workshop_protocol_orchestrator.prepare_workshop())
    with TestClient(app) as client:
        with client.websocket_connect("/ws/presence"):
            assert app.state.client_presence.has_active_clients() is True
            with client.websocket_connect("/ws/live") as live:
                assert live.receive_json() == {"type": "CALL_STATE", "state": "CONNECTING"}
                assert live.receive_json()["state"] == "DISCONNECTED"
                live.send_json({"type": "END"})
            assert app.state.workshop_protocol_foundation.preparation_projection(
                app.state.bootstrap.case_id
            )["cleanup_state"] == "NOT_REQUIRED"
            with client.websocket_connect("/ws/presence"):
                assert app.state.workshop_protocol_foundation.preparation_projection(
                    app.state.bootstrap.case_id
                )["cleanup_state"] == "NOT_REQUIRED"
            assert app.state.client_presence.has_active_clients() is True
            assert app.state.workshop_protocol_foundation.preparation_projection(
                app.state.bootstrap.case_id
            )["cleanup_state"] == "NOT_REQUIRED"
        assert app.state.client_presence.has_active_clients() is False
        assert app.state.workshop_protocol_foundation.preparation_projection(
            app.state.bootstrap.case_id
        )["cleanup_state"] == "RESTART_GRACE"

        with client.websocket_connect("/ws/presence"):
            assert app.state.workshop_protocol_foundation.preparation_projection(
                app.state.bootstrap.case_id
            )["cleanup_state"] == "NOT_REQUIRED"


def test_expired_grace_waits_for_analyzer_job_terminal_state(tmp_path):
    class MutableClock:
        value = NOW

        def now(self):
            return self.value

        def advance(self, seconds: int):
            self.value += timedelta(seconds=seconds)

    async def scenario():
        clock = MutableClock()
        adapter = CancellablePreparationAdapter("never")
        app = create_app(
            settings=configured(tmp_path),
            clock=clock,
            source_catalog=SourceCatalog(ROOT),
            live_provider=object(),
            analyzer_adapter=adapter,
        )
        orchestrator = app.state.workshop_protocol_orchestrator
        foundation = app.state.workshop_protocol_foundation
        await orchestrator.prepare_workshop()
        await orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=1,
                text="A committed final turn still needs its Analyzer stage.",
                provider_request_id="grace-terminal-gate-turn",
                speaker_actor_id=foundation.case_actor(app.state.bootstrap.case_id, "PM"),
                actor="PM",
            )
        )
        assert foundation.analyzer_jobs(app.state.bootstrap.case_id)[0][
            "state"
        ] == "ANALYSIS_PENDING"
        await orchestrator.note_last_client_disconnected()
        clock.advance(900)

        assert await orchestrator.expire_restart_grace() is False
        assert foundation.active_analyzer_context(app.state.bootstrap.case_id) is not None

        worker = DurableAnalyzerWorker(orchestrator)
        assert await worker.run_once() is True
        assert foundation.analyzer_jobs(app.state.bootstrap.case_id)[0]["state"] == "COMPLETED"
        assert foundation.active_analyzer_context(app.state.bootstrap.case_id) is not None

        assert await worker.run_once() is True
        assert foundation.active_analyzer_context(app.state.bootstrap.case_id) is None
        assert foundation.preparation_projection(app.state.bootstrap.case_id)[
            "cleanup_state"
        ] == "COMPLETED"

    asyncio.run(scenario())


def test_cleanup_clears_only_confirmed_ids_then_retries_same_uncertain_id(tmp_path):
    class MutableNow:
        value = NOW

        def __call__(self):
            return self.value

        def advance(self, seconds: int):
            self.value += timedelta(seconds=seconds)

    class PartialCleanupAdapter:
        calls = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def release_resource_ids(
            self,
            conversation_id,
            file_ids,
            *,
            response_id=None,
            response_ids=(),
        ):
            self.calls += 1
            if self.calls == 1:
                assert response_ids == ("resp_turn_cleanup",)
                return ProviderCleanupReceipt(
                    deletions=(
                        ProviderResourceDeletion(
                            "RESPONSE",
                            response_id,
                            "delete-response",
                            None,
                            "UNCONFIRMED",
                            "DELETE_TIMEOUT",
                            True,
                            60_000,
                        ),
                        ProviderResourceDeletion(
                            "RESPONSE",
                            response_ids[0],
                            "delete-turn-response",
                            None,
                            "DELETED",
                            None,
                            False,
                            2,
                        ),
                        ProviderResourceDeletion(
                            "CONVERSATION", conversation_id, "delete-conversation", None,
                            "DELETED", None, False, 2,
                        ),
                        ProviderResourceDeletion(
                            "FILE", file_ids[0], "delete-file-pm", None,
                            "DELETED", None, False, 3,
                        ),
                        ProviderResourceDeletion(
                            "FILE", file_ids[1], "delete-file-technical", None,
                            "DELETED", None, False, 4,
                        ),
                    )
                )
            assert conversation_id is None
            assert file_ids == ()
            assert response_id == "resp_cleanup"
            assert response_ids == ()
            return ProviderCleanupReceipt(
                deletions=(
                    ProviderResourceDeletion(
                        "RESPONSE",
                        "resp_cleanup",
                        "delete-response",
                        None,
                        "ALREADY_ABSENT",
                        None,
                        False,
                        4,
                    ),
                )
            )

    async def scenario():
        url, original = _runtime(tmp_path)
        clock = MutableNow()
        foundation = type(original)(url, now=clock)
        foundation.test_source_set = original.test_source_set
        foundation.checkpoint_preparation_resource(
            CASE_ID,
            pm_file_id="file_pm",
            technical_file_id="file_technical",
            provider_conversation_id="conversation_cleanup",
            bootstrap_response_id="resp_cleanup",
        )
        foundation.checkpoint_provider_response(
            CASE_ID,
            client_request_id="specops-turn_analysis-cleanup",
            operation="TURN_ANALYSIS",
            provider_response_id="resp_turn_cleanup",
        )
        foundation.set_preparation_phase(
            CASE_ID, "FAILED", failure_code="ABANDONED", cleanup_state="PENDING"
        )
        _activate(foundation)
        analyzer_contract = foundation.active_analyzer_context(CASE_ID).analyzer_contract
        values = _base(foundation.case_revision(CASE_ID))
        values.update(
            command_type="INVALIDATE_ANALYZER_CONTEXT",
            context_id=CONTEXT_ID,
            reason_code="WORKSHOP_CLOSED",
        )
        foundation.execute(c.InvalidateAnalyzerContextCommand(**values))
        adapter = PartialCleanupAdapter()
        orchestrator = V4ProductionOrchestrator(
            foundation=foundation,
            adapter=adapter,
            case_id=CASE_ID,
            session_id=foundation.get_case(CASE_ID).session_id,
            sources=(),
            analyzer_contract=analyzer_contract,
            now=clock,
        )

        first = await orchestrator.cleanup_preparation()
        assert first["cleanup_state"] == "RETRY_WAIT"
        assert first["cleanup_last_error_code"] == "DELETE_TIMEOUT"
        remaining = foundation.preparation_resources(CASE_ID)
        assert remaining["provider_conversation_id"] is None
        assert remaining["pm_file_id"] is None
        assert remaining["technical_file_id"] is None
        assert remaining["bootstrap_response_id"] == "resp_cleanup"
        assert foundation.pending_provider_responses(CASE_ID) == ()

        clock.advance(30)
        second = await orchestrator.cleanup_preparation()
        assert second["cleanup_state"] == "COMPLETED"
        assert adapter.calls == 2
        assert (
            foundation.preparation_resources(CASE_ID)["bootstrap_response_id"]
            is None
        )

    asyncio.run(scenario())


def test_cancelled_analysis_lease_reclaims_provider_completed_stage_without_another_call(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    context = foundation.active_analyzer_context(CASE_ID)
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=SimpleNamespace(source_set_hash=lambda sources: SOURCE_SET_HASH),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    transcript_input = FinalTranscriptInput(
        turn_sequence=1,
        text="The final participant answer is recorded.",
        provider_request_id="task-27-replay-stage",
        speaker_actor_id=foundation.case_actor(CASE_ID, "PM"),
        actor="PM",
    )
    first = asyncio.run(orchestrator.record_final_transcript(transcript_input))
    assert first.analysis is None and first.duplicate is False
    event = foundation.latest_final_transcript(CASE_ID)
    snapshot = foundation.semantic_snapshot(CASE_ID)
    job = foundation.claim_analyzer_job(CASE_ID, worker_id="crashed-worker")
    request = c.AnalyzeFinalTurnRequest.model_construct(
        **orchestrator._provider_base(
            operation=c.AnalyzerOperation.TURN_ANALYSIS,
            operation_key=job["job_id"],
            context=context,
            based_on_revision=snapshot.case_revision,
        ),
        request_hash="sha256:" + "0" * 64,
        transcript=c.FinalizedTranscriptInput(
            transcript_event_id=event.event_id,
            transcript_hash=event.transcript_hash,
            speaker_actor_id=event.speaker_actor_id,
            actor=event.actor,
            sequence_number=event.sequence_number,
            text=event.text,
        ),
        prior_transcript=None,
        foundation_snapshot=snapshot,
        requested_output="TURN_ANALYSIS_CANDIDATE",
    )
    from specops_contracts.canonical import analyzer_request_hash

    request = request.model_copy(
        update={"request_hash": analyzer_request_hash(request.model_dump(mode="json"))}
    )
    candidate = c.TurnAnalysisCandidate(
        protocol_version="1.0.0",
        output_type="TURN_ANALYSIS_CANDIDATE",
        analyzer_run_id=request.analyzer_run_id,
        context_id=request.context_id,
        request_hash=request.request_hash,
        source_set_hash=request.source_set_hash,
        transcript_event_id=event.event_id,
        based_on_case_revision=request.based_on_case_revision,
        disposition=c.TurnDisposition.NO_SEMANTIC_CHANGE,
        no_change_reason_code="SOCIAL_ONLY",
        evidence_candidates=(),
        new_problems=(),
        new_problem_clusters=(),
        revised_problem_clusters=(),
        new_questions=(),
        revised_questions=(),
        low_risk_facts=(),
        decisions=(),
        problem_assessments=(),
        evidence_findings=(),
    )
    foundation.checkpoint_analyzer_job(
        job["job_id"],
        worker_id="crashed-worker",
        state="PROVIDER_COMPLETED",
        request_json=request.model_dump_json(),
        candidate_json=candidate.model_dump_json(),
    )
    table = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
    with foundation.engine.begin() as connection:
        connection.execute(
            table.update()
            .where(table.c.job_id == job["job_id"])
            .values(lease_expires_at="2026-08-12T11:59:00Z")
        )

    class NoProviderReplay:
        calls = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute(self, request, *, context):
            self.calls += 1
            raise AssertionError("provider-completed stage must not call the provider again")

    orchestrator.adapter = NoProviderReplay()
    worker = DurableAnalyzerWorker(orchestrator, worker_id="restart-worker")
    assert asyncio.run(worker.run_once()) is True
    completed = foundation.analyzer_jobs(CASE_ID)[0]
    assert completed["state"] == "COMPLETED"
    assert completed["admission_receipt_json"] is not None
    assert orchestrator.adapter.calls == 0
    replay = asyncio.run(orchestrator.record_final_transcript(transcript_input))
    assert replay.duplicate is True
    assert replay.analysis is not None


def test_real_analysis_cancellation_with_unknown_outcome_is_not_retried(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    context = foundation.active_analyzer_context(CASE_ID)

    class CancellableAnalysisAdapter:
        attempts = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute(self, request, *, context):
            self.attempts += 1
            raise asyncio.CancelledError

    adapter = CancellableAnalysisAdapter()
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    transcript_input = FinalTranscriptInput(
        turn_sequence=1,
        text="This answer survives a cancelled Analyzer attempt.",
        provider_request_id="task-27-real-analysis-cancel",
        speaker_actor_id=foundation.case_actor(CASE_ID, "PM"),
        actor="PM",
    )
    asyncio.run(orchestrator.record_final_transcript(transcript_input))
    worker = DurableAnalyzerWorker(orchestrator, worker_id="cancelled-analysis-worker")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run_once())
    failed = foundation.analyzer_jobs(CASE_ID)[0]
    assert failed["state"] == "FAILED"
    assert failed["last_error_code"] == "PROVIDER_OUTCOME_UNCERTAIN_CANCELLED"
    resumed = DurableAnalyzerWorker(orchestrator, worker_id="resumed-analysis-worker")
    assert asyncio.run(resumed.run_once()) is False
    assert adapter.attempts == 1
    replay = asyncio.run(orchestrator.record_final_transcript(transcript_input))
    assert replay.duplicate is True
    assert replay.analysis is None


def test_provider_timeout_is_terminal_without_blind_retry(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    context = foundation.active_analyzer_context(CASE_ID)

    class TimeoutAdapter:
        calls = 0

        @staticmethod
        def source_set_hash(sources):
            return SOURCE_SET_HASH

        async def execute(self, request, *, context):
            self.calls += 1
            raise ProviderAdapterError(
                c.ProviderFailureReceipt(
                    provider=c.ProviderName.OPENAI,
                    stage=c.ProviderProcessingStage.TURN_ANALYSIS,
                    client_request_id=request.client_request_id,
                    provider_request_id=None,
                    status_code=None,
                    code=c.ProviderFailureCode.TIMEOUT,
                    retryable=True,
                    validation_diagnostics=(),
                    occurred_at=NOW,
                )
            )

    adapter = TimeoutAdapter()
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    asyncio.run(
        orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=1,
                text="This answer receives one bounded Analyzer attempt.",
                provider_request_id="task-27-timeout-no-retry",
                speaker_actor_id=foundation.case_actor(CASE_ID, "PM"),
                actor="PM",
            )
        )
    )
    worker = DurableAnalyzerWorker(orchestrator, worker_id="timeout-worker")
    assert asyncio.run(worker.run_once()) is True
    failed = foundation.analyzer_jobs(CASE_ID)[0]
    assert failed["state"] == "FAILED"
    assert failed["last_error_code"] == "PROVIDER_OUTCOME_UNCERTAIN_TIMEOUT"
    assert asyncio.run(worker.run_once()) is False
    assert adapter.calls == 1


@pytest.mark.parametrize(
    ("failure_code", "resources", "cleanup_state"),
    (
        ("CANCELLED_DURING_FILE_UPLOAD", {"pm_file_id": "file_pm"}, "NOT_REQUIRED"),
        (
            "CANCELLED_DURING_CONVERSATION_CREATION",
            {"pm_file_id": "file_pm", "technical_file_id": "file_technical"},
            "NOT_REQUIRED",
        ),
        (
            "CANCELLED_DURING_BOOTSTRAP",
            {
                "pm_file_id": "file_pm",
                "technical_file_id": "file_technical",
                "provider_conversation_id": "conversation_one",
            },
            "NOT_REQUIRED",
        ),
        (
            "CANCELLED_DURING_CLEANUP",
            {
                "pm_file_id": "file_pm",
                "technical_file_id": "file_technical",
                "provider_conversation_id": "conversation_one",
            },
            "PENDING",
        ),
    ),
)
def test_named_preparation_cancellations_preserve_resumable_checkpoints(
    tmp_path, failure_code, resources, cleanup_state
):
    _, foundation = _runtime(tmp_path)
    foundation.checkpoint_preparation_resource(CASE_ID, **resources)
    foundation.set_preparation_phase(
        CASE_ID,
        "FAILED",
        failure_code=failure_code,
        cleanup_state=cleanup_state,
    )
    restarted = type(foundation)(
        foundation.engine.url.render_as_string(hide_password=False), now=lambda: NOW
    )
    recovered = restarted.preparation_resources(CASE_ID)
    assert all(recovered[key] == value for key, value in resources.items())
    assert restarted.preparation_projection(CASE_ID)["failure_code"] == failure_code
    restarted.set_preparation_phase(CASE_ID, "VALIDATING_DOCUMENTS")
    resumed = restarted.preparation_projection(CASE_ID)
    assert resumed["phase"] == "VALIDATING_DOCUMENTS"
    assert resumed["failure_code"] is None


def test_preparation_resource_checkpoints_and_failed_cleanup_are_reconstructable(tmp_path):
    _, foundation = _runtime(tmp_path)
    foundation.checkpoint_preparation_resource(CASE_ID, pm_file_id="file_pm")
    first = foundation.preparation_resources(CASE_ID)
    assert first["pm_file_id"] == "file_pm"
    assert first["technical_file_id"] is None
    foundation.checkpoint_preparation_resource(
        CASE_ID,
        technical_file_id="file_technical",
        provider_conversation_id="conversation_one",
    )
    foundation.set_preparation_phase(
        CASE_ID,
        "FAILED",
        failure_code="CANCELLED_DURING_BOOTSTRAP",
        cleanup_state="PENDING",
    )
    restarted = type(foundation)(foundation.engine.url.render_as_string(hide_password=False), now=lambda: NOW)
    recovered = restarted.preparation_resources(CASE_ID)
    projection = restarted.preparation_projection(CASE_ID)
    assert recovered["pm_file_id"] == "file_pm"
    assert recovered["technical_file_id"] == "file_technical"
    assert recovered["provider_conversation_id"] == "conversation_one"
    assert projection["cleanup_state"] == "PENDING"
    assert projection["failure_code"] == "CANCELLED_DURING_BOOTSTRAP"


def test_button_completion_endpoint_is_durable_idempotent_and_closes_new_turns(tmp_path):
    adapter = CancellablePreparationAdapter("never")
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=adapter,
    )
    with TestClient(app) as client:
        for _ in range(200):
            if client.get("/api/workshop").json()["preparation"]["phase"] == "READY":
                break
            import time

            time.sleep(0.01)
        first = client.post(
            "/api/v4/workshop/complete",
            json={"operation_key": "browser-finish-first"},
        )
        replay = client.post(
            "/api/v4/workshop/complete",
            json={"operation_key": "browser-finish-repeated"},
        )
        rejected = client.post(
            "/api/session/final-turn",
            json={
                "turn_sequence": 1,
                "text": "This must not reopen a completed Workshop.",
                "provider_request_id": "post-completion-turn",
                "correction_of_version": None,
            },
        )
        projection = client.get("/api/workshop").json()

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["receipt"]["completion_source"] == "BUTTON"
    assert first.json()["receipt"]["command"]["command_id"] == replay.json()["receipt"][
        "command"
    ]["command_id"]
    assert replay.json()["replayed"] is True
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "INVALID_TRANSITION"
    assert projection["session"]["conversation_phase"] == "COMPLETE"
    assert projection["session"]["revision_locked"] is True
    assert projection["completion"]["completed_at"] is not None


def test_explicit_voice_completion_binds_final_transcript_and_finishes_analysis(tmp_path):
    async def scenario():
        adapter = CancellablePreparationAdapter("never")
        app = create_app(
            settings=configured(tmp_path),
            source_catalog=SourceCatalog(ROOT),
            live_provider=object(),
            analyzer_adapter=adapter,
        )
        orchestrator = app.state.workshop_protocol_orchestrator
        foundation = app.state.workshop_protocol_foundation
        await orchestrator.prepare_workshop()
        transcript = await orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=1,
                text="I have finished the Spec Workshop.",
                provider_request_id="voice-explicit-completion",
                speaker_actor_id=foundation.case_actor(app.state.bootstrap.case_id, "PM"),
                actor="PM",
            )
        )
        completion = await orchestrator.complete_from_final_transcript(
            transcript.transcript.transcript_event_id
        )
        assert completion.action == "COMPLETE"
        assert completion.completion.state == "FINISHING_ANALYSIS"
        assert (
            completion.completion.receipt.completion_transcript_event_id
            == transcript.transcript.transcript_event_id
        )
        assert foundation.preparation_projection(app.state.bootstrap.case_id)[
            "cleanup_state"
        ] == "FINISHING_ANALYSIS"

        worker = DurableAnalyzerWorker(orchestrator)
        await worker.drain()
        assert foundation.analyzer_jobs(app.state.bootstrap.case_id)[0]["state"] == "COMPLETED"
        assert foundation.workshop_completion_projection(app.state.bootstrap.case_id)[
            "state"
        ] == "COMPLETE"
        assert foundation.active_analyzer_context(app.state.bootstrap.case_id) is None
        assert adapter.cleanup_attempts == 1
        assert "resp_bootstrap" in adapter.cleaned_resources

    asyncio.run(scenario())


def test_ambiguous_then_affirmative_voice_completion_binds_adjacent_turns(tmp_path):
    async def scenario():
        adapter = CancellablePreparationAdapter("never")
        app = create_app(
            settings=configured(tmp_path),
            source_catalog=SourceCatalog(ROOT),
            live_provider=object(),
            analyzer_adapter=adapter,
        )
        orchestrator = app.state.workshop_protocol_orchestrator
        foundation = app.state.workshop_protocol_foundation
        await orchestrator.prepare_workshop()
        actor = foundation.case_actor(app.state.bootstrap.case_id, "PM")
        intent = await orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=1,
                text="I think we're done.",
                provider_request_id="voice-ambiguous-completion",
                speaker_actor_id=actor,
                actor="PM",
            )
        )
        pending = await orchestrator.complete_from_final_transcript(
            intent.transcript.transcript_event_id
        )
        assert pending.action == "CONFIRMATION_REQUIRED"
        assert foundation.preparation_projection(app.state.bootstrap.case_id)[
            "workshop_complete_at"
        ] is None

        confirmation = await orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=2,
                text="Yes, I confirm.",
                provider_request_id="voice-confirmed-completion",
                speaker_actor_id=actor,
                actor="PM",
            )
        )
        completed = await orchestrator.complete_from_final_transcript(
            confirmation.transcript.transcript_event_id
        )
        assert completed.action == "COMPLETE"
        assert completed.completion.receipt.completion_source == "VOICE_CONFIRMED"
        assert (
            completed.completion.receipt.completion_transcript_event_id
            == intent.transcript.transcript_event_id
        )
        assert (
            completed.completion.receipt.confirmation_transcript_event_id
            == confirmation.transcript.transcript_event_id
        )

    asyncio.run(scenario())
