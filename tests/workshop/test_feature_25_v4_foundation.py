from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from specops_contracts import workshop_v1 as c
from specops_contracts.canonical import payload_hash, transcript_hash
from specops_workflow import WorkflowService, migrate
from specops_workflow.enums import SourceArtifactType
from specops_workflow.models import CreateCaseCommand, RegisterSourceArtifactCommand, SourceArtifactIdentity
from specops_workflow.persistence import WORKSHOP_PROTOCOL_TABLES, audit_events, cases, engine_for
from specops_workflow.workshop_protocol import FoundationProtocolError, WorkshopFoundationService
from specops_workshop.v4.api import install_workshop_protocol_api


NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
CASE_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("10000000-0000-4000-8000-000000000002")
PM_ID = UUID("10000000-0000-4000-8000-000000000003")
DEV_ID = UUID("10000000-0000-4000-8000-000000000004")
CONTEXT_ID = UUID("10000000-0000-4000-8000-000000000005")
RUN_ID = UUID("10000000-0000-4000-8000-000000000006")
SOURCE_SET_HASH = "sha256:" + "1" * 64
REQUEST_HASH = "sha256:" + "2" * 64


def _contract() -> c.AnalyzerContractBinding:
    return c.AnalyzerContractBinding(
        protocol_version="1.0.0",
        instruction_set_id="specops-workshop-analyzer",
        instruction_set_version=1,
        instruction_set_hash="sha256:" + "3" * 64,
        semantic_quality_contract_id="SEMANTIC-QUALITY-CONTRACT",
        semantic_quality_contract_version="2.0.0",
        semantic_quality_contract_hash="sha256:" + "4" * 64,
        provider_schema_version="1.0.0",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
    )


def _source_set(locators: tuple[str, str], hashes: tuple[str, str]) -> c.SourceSetBinding:
    return c.SourceSetBinding(
        source_set_hash=SOURCE_SET_HASH,
        ordered_sources=tuple(
            c.ProviderSourceBinding(
                source=c.SourceIdentity(
                    source_id=UUID(f"10000000-0000-4000-8000-{index:012d}"),
                    role=role,
                    version=1,
                    payload_hash="sha256:" + hashes[index - 5],
                    canonical_locator=locators[index - 5],
                    filename=f"{index}.md",
                    media_type="text/markdown",
                ),
                provider_file_id=f"file_{index}",
            )
            for index, role in (
                (5, c.SourceRole.PM_SPEC),
                (6, c.SourceRole.TECHNICAL_CONTRACT),
            )
        ),
    )


def _base(revision: int, *, actor="SYSTEM", idempotency=None) -> dict:
    return {
        "protocol_version": "1.0.0",
        "command_id": uuid4(),
        "case_id": CASE_ID,
        "session_id": SESSION_ID,
        "correlation_id": uuid4(),
        "causation_id": None,
        "idempotency_key": idempotency or f"idem-{uuid4()}",
        "issued_at": NOW,
        "acting_actor_id": actor,
        "expected_case_revision": revision,
    }


def _transcript_command(revision: int, sequence: int, text: str, actor=PM_ID):
    event_id = uuid4()
    values = _base(revision)
    values.update(
        command_type="RECORD_FINAL_TRANSCRIPT",
        transcript=c.TranscriptFinalizedEvent(
            protocol_version="1.0.0",
            event_type="TRANSCRIPT_FINALIZED",
            event_id=event_id,
            case_id=CASE_ID,
            session_id=SESSION_ID,
            correlation_id=values["correlation_id"],
            causation_id=None,
            event_sequence=sequence,
            observed_case_revision=revision,
            occurred_at=NOW,
            producer="VOICE",
            turn_id=uuid4(),
            transcript_artifact_id=uuid4(),
            transcript_version=1,
            transcript_hash=transcript_hash(text),
            actor=c.TranscriptActor.PM,
            speaker_actor_id=actor,
            speaker_attribution_method=c.SpeakerAttributionMethod.VERBAL_SELF_ASSERTION,
            sequence_number=sequence,
            text=text,
        ),
    )
    return c.RecordFinalTranscriptCommand(**values)


def _runtime(tmp_path):
    url = f"sqlite:///{tmp_path / 'foundation.sqlite'}"
    migrate(url)
    legacy = WorkflowService(database_url=url)
    legacy.create_case(
        CreateCaseCommand(
            command_id=uuid4(),
            case_id=CASE_ID,
            acting_actor_id=PM_ID,
            pm_actor_id=PM_ID,
            dev_lead_actor_id=DEV_ID,
        )
    )
    pm_path = tmp_path / "pm-spec.md"
    technical_path = tmp_path / "technical-contract.md"
    pm_path.write_text("Export filtered orders.\n", encoding="utf-8")
    technical_path.write_text(
        "\n" * 530 + "CSV output uses UTF-8 and one header row.\n",
        encoding="utf-8",
    )
    locators = (str(pm_path), str(technical_path))
    hashes = tuple(hashlib.sha256(Path(value).read_bytes()).hexdigest() for value in locators)
    revision = 1
    for index, (locator, digest, source_type) in enumerate(
        zip(
            locators,
            hashes,
            (SourceArtifactType.BUSINESS_SPEC, SourceArtifactType.TECHNICAL_CONTRACT),
        ),
        start=5,
    ):
        result = legacy.register_source_artifact(
            RegisterSourceArtifactCommand(
                command_id=uuid4(),
                case_id=CASE_ID,
                acting_actor_id="SYSTEM",
                expected_case_revision=revision,
                identity=SourceArtifactIdentity(
                    artifact_id=UUID(f"10000000-0000-4000-8000-{index:012d}"),
                    case_id=CASE_ID,
                    type=source_type,
                    version=1,
                    media_type="text/markdown",
                    canonical_locator=locator,
                    content_hash=digest,
                ),
            )
        )
        revision = result.receipt.revision
    foundation = WorkshopFoundationService(url, now=lambda: NOW)
    foundation.test_source_set = _source_set(locators, hashes)
    foundation.register_case(
        case_id=CASE_ID,
        session_id=SESSION_ID,
        source_set_hash=SOURCE_SET_HASH,
    )
    return url, foundation


def _activate(foundation):
    values = _base(3)
    values.update(
        command_type="ACTIVATE_ANALYZER_CONTEXT",
        context=c.AnalyzerContextBinding(
            protocol_version="1.0.0",
            context_id=CONTEXT_ID,
            session_id=SESSION_ID,
            provider=c.ProviderName.OPENAI,
            provider_conversation_id="conv_test",
            bootstrap_response_id="resp_test",
            model="gpt-5.6-terra",
            reasoning_effort=c.ReasoningEffort.MEDIUM,
            conversation_state_persisted=True,
            response_store_enabled=True,
            analyzer_contract=_contract(),
            source_set=foundation.test_source_set,
            status=c.ContextStatus.ACTIVE,
            created_at=NOW,
            invalidated_at=None,
            invalidation_reason=None,
        ),
    )
    return foundation.execute(c.ActivateAnalyzerContextCommand(**values))


def _turn_candidate(transcript_id: UUID, revision: int) -> c.TurnAnalysisCandidate:
    evidence = c.CandidateEntityRef(ref_kind="CANDIDATE_KEY", candidate_key="evidence-encoding")
    problem = c.CandidateEntityRef(ref_kind="CANDIDATE_KEY", candidate_key="problem-encoding")
    decisions = tuple(
        c.DecisionCandidate(
            candidate_key=f"decision-{suffix}",
            existing_decision_ref=None,
            classification=c.Domain.PRODUCT,
            statement=statement,
            rationale="This closes one explicit export decision.",
            alternatives_considered=(),
            problem_links=(
                c.ProblemResolutionLink(problem_ref=problem, resolution_kind=c.ProblemResolutionKind.FULL),
            ),
            evidence_refs=(evidence,),
            requires_human_confirmation=True,
        )
        for suffix, statement in (
            ("encoding", "Use UTF-8 for every CSV export."),
            ("header", "Emit exactly one header row."),
        )
    )
    return c.TurnAnalysisCandidate(
        protocol_version="1.0.0",
        output_type="TURN_ANALYSIS_CANDIDATE",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        transcript_event_id=transcript_id,
        based_on_case_revision=revision,
        disposition=c.TurnDisposition.SUBSTANTIVE,
        no_change_reason_code=None,
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-encoding",
                source_role=c.SourceRole.TECHNICAL_CONTRACT,
                locator=c.SourceLineLocator(locator_kind=c.SourceLocatorKind.SOURCE_LINES, start_line=531, end_line=531),
                relevance_claim="The source fixes export encoding and headers.",
                quoted_text_candidate="CSV output uses UTF-8 and one header row.",
            ),
        ),
        new_problems=(
            c.TurnProblemCandidate(
                candidate_key="problem-encoding",
                problem_kind=c.ProblemKind.MISSING_DECISION,
                domain=c.Domain.PRODUCT,
                severity=c.Severity.HIGH,
                statement="The export encoding and header policy need confirmation.",
                consequence="Consumers could parse files inconsistently.",
                evidence_refs=(evidence,),
            ),
        ),
        new_problem_clusters=(),
        revised_problem_clusters=(),
        new_questions=(),
        revised_questions=(),
        low_risk_facts=(),
        decisions=decisions,
        problem_assessments=(),
        evidence_findings=(
            c.SemanticEvidenceFindingCandidate(
                candidate_key="finding-encoding",
                claim_ref=problem,
                evidence_ref=evidence,
                assessment=c.EvidenceAssessment.SUPPORTS,
                confidence=0.99,
                explanation="The exact source line supports the export decision context.",
            ),
        ),
    )


def test_foundation_mixed_batch_is_atomic_audited_replayable_and_restart_safe(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)

    turn = _transcript_command(4, 1, "Confirm UTF-8 and one header row.")
    foundation.execute(turn)
    candidate = _turn_candidate(turn.transcript.event_id, 5)
    admit_values = _base(5)
    admit_values.update(
        command_type="ADMIT_TURN_ANALYSIS",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        provider_request_hash=REQUEST_HASH,
        candidate=candidate,
    )
    admitted = foundation.execute(c.AdmitTurnAnalysisCommand(**admit_values))
    decision_ids = tuple(
        item.foundation_id for item in admitted.identity_mappings if item.entity_kind == "DECISION"
    )
    review_values = _base(6)
    review_values.update(
        command_type="MATERIALIZE_DECISION_BATCH_REVIEW",
        pending_decision_ids=decision_ids,
        derived_from_cluster_ids=(),
    )
    review_receipt = foundation.execute(c.MaterializeDecisionBatchReviewCommand(**review_values))
    review = foundation.current_decision_view(CASE_ID)
    assert review is not None
    assert review.view_hash == review_receipt.view_hash
    assert [item.handle for item in review.items] == ["A", "B"]

    confirmation = _transcript_command(7, 2, "Confirm A and revise B.")
    foundation.execute(confirmation)
    selections = (
        c.VoiceConfirmationSelectionItemCandidate(handle="A", action=c.ConfirmationAction.CONFIRM, revision_span=None),
        c.VoiceConfirmationSelectionItemCandidate(
            handle="B",
            action=c.ConfirmationAction.REVISE,
            revision_span=c.TranscriptSpan(
                transcript_event_id=confirmation.transcript.event_id,
                start_character=14,
                end_character_exclusive=22,
            ),
        ),
    )
    selection = c.VoiceConfirmationSelectionCandidate(
        protocol_version="1.0.0",
        output_type="VOICE_CONFIRMATION_SELECTION_CANDIDATE",
        producer="VOICE",
        selection_event_id=uuid4(),
        mapping_status=c.ConfirmationMappingStatus.MAPPED,
        decision_batch_view_id=review.view_id,
        decision_batch_view_hash=review.view_hash,
        observed_case_revision=8,
        transcript_event_id=confirmation.transcript.event_id,
        speaker_actor_id=PM_ID,
        selections=selections,
        unmentioned_item_policy="REMAIN_PENDING",
        clarification_question=None,
    )
    response_values = _base(0, actor=PM_ID)
    response_values.pop("expected_case_revision")
    response_values.update(
        command_type="APPLY_DECISION_BATCH_RESPONSE",
        observed_case_revision=8,
        actor_authentication=c.VerbalSelfAssertion(
            authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
            assurance_level=c.AssuranceLevel.SELF_ASSERTED,
            actor_id=PM_ID,
            asserted_display_name="PM",
            claimed_role="Product Manager",
            assertion_transcript_event_id=confirmation.transcript.event_id,
        ),
        selection=selection,
        decision_batch_view_id=review.view_id,
        decision_batch_view_hash=review.view_hash,
        response_transcript_event_id=confirmation.transcript.event_id,
        item_actions=tuple(
            c.DecisionBatchItemActionCommand(
                review_item_id=item.review_item_id,
                handle=item.handle,
                pending_decision_id=item.pending_decision_id,
                expected_pending_decision_version=item.pending_decision_version,
                action=selections[index].action,
                revision_span=selections[index].revision_span,
            )
            for index, item in enumerate(review.items)
        ),
        unmentioned_item_policy="REMAIN_PENDING",
    )
    command = c.ApplyDecisionBatchResponseCommand(**response_values)
    receipt = foundation.execute(command)
    assert [item.outcome for item in receipt.item_results] == ["COMMITTED", "REVISION_REQUESTED"]
    assert foundation.execute(command) == receipt

    engine = engine_for(url)
    with engine.connect() as connection:
        assert connection.execute(select(cases.c.revision).where(cases.c.id == str(CASE_ID))).scalar_one() == 9
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 9
        assert connection.execute(
            select(func.count()).select_from(WORKSHOP_PROTOCOL_TABLES["workshop_protocol_events"])
        ).scalar_one() == 6
        stored = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.payload_json).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.entity_kind == "DECISION"
            )
        ).scalars().all()
        assert stored and all("CANDIDATE_KEY" not in value for value in stored)
        finding_json = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.payload_json).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_semantic_records"].c.entity_kind == "FINDING"
            )
        ).scalar_one()
        finding = c.AdmittedSemanticEvidenceFinding.model_validate_json(finding_json)
        assert finding.assessment is c.EvidenceAssessment.SUPPORTS
        assert finding.source_hash == foundation.test_source_set.ordered_sources[1].source.payload_hash

    restarted = WorkflowService(database_url=url)
    assert restarted._cases[CASE_ID].revision == 9

    duplicate = command.model_copy(
        update={"command_id": uuid4(), "idempotency_key": f"idem-{uuid4()}"}
    )
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(duplicate)
    assert error.value.code is c.FoundationRejectionCode.STALE_VIEW
    with engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 9


def test_transcript_hash_and_idempotency_conflicts_fail_without_mutation(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    command = _transcript_command(4, 1, "Exact final transcript.")
    bad_event = command.transcript.model_copy(update={"transcript_hash": "sha256:" + "9" * 64})
    bad = command.model_copy(update={"transcript": bad_event})
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(bad)
    assert error.value.code is c.FoundationRejectionCode.TRANSCRIPT_BINDING_FAILED

    applied = foundation.execute(command)
    conflicting_event = command.transcript.model_copy(
        update={"text": "Different content.", "transcript_hash": transcript_hash("Different content.")}
    )
    conflict = command.model_copy(update={"transcript": conflicting_event})
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(conflict)
    assert error.value.code is c.FoundationRejectionCode.DUPLICATE_CONFLICT

    with engine_for(url).connect() as connection:
        assert connection.execute(select(cases.c.revision).where(cases.c.id == str(CASE_ID))).scalar_one() == 5
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 5
    assert applied.command.resulting_case_revision == 5


def test_voice_and_generic_http_endpoints_share_the_foundation_handler(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    app = FastAPI()
    install_workshop_protocol_api(app, foundation)
    command = _transcript_command(4, 1, "The same final transcript enters Foundation.")
    with TestClient(app) as client:
        voice = client.post(
            "/api/v4/voice/final-transcripts",
            json=command.model_dump(mode="json", exclude_none=False),
        )
        assert voice.status_code == 200, voice.json()
        generic = client.post(
            "/api/v4/foundation/commands",
            json=command.model_dump(mode="json", exclude_none=False),
        )
        assert generic.status_code == 200
        assert generic.json() == voice.json()

        conflict = command.model_copy(
            update={
                "transcript": command.transcript.model_copy(
                    update={
                        "text": "Conflicting transcript.",
                        "transcript_hash": transcript_hash("Conflicting transcript."),
                    }
                )
            }
        )
        rejected = client.post(
            "/api/v4/foundation/commands",
            json=conflict.model_dump(mode="json", exclude_none=False),
        )
        assert rejected.status_code == 409
        assert rejected.json() == {"detail": {"code": "DUPLICATE_CONFLICT"}}


def test_artifact_confirmation_binds_exact_view_commits_once_and_survives_refresh(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    artifact_id, view_id, confirmation_id = uuid4(), uuid4(), uuid4()
    digest = payload_hash({})
    tables = WORKSHOP_PROTOCOL_TABLES
    with engine_for(url).begin() as connection:
        connection.execute(
            insert(tables["workshop_artifact_records"]).values(
                artifact_id=str(artifact_id),
                artifact_version=1,
                case_id=str(CASE_ID),
                artifact_type="SPEC_PACKAGE",
                artifact_key="SPEC-TEST",
                record_revision=1,
                payload_hash=digest,
                payload_json="{}",
                governance_json="{}",
                status="DRAFT",
                confirmed_from_json=None,
                created_at=NOW.isoformat(),
            )
        )
        connection.execute(
            insert(tables["workshop_artifact_reviews"]).values(
                view_id=str(view_id),
                confirmation_id=str(confirmation_id),
                case_id=str(CASE_ID),
                artifact_id=str(artifact_id),
                artifact_version=1,
                record_revision=1,
                payload_hash=digest,
                view_hash="sha256:" + "a" * 64,
                view_mode="FINAL",
                view_json='{"exact":"displayed review"}',
                current=1,
                confirmed=0,
                generated_at=NOW.isoformat(),
            )
        )

    transcript = _transcript_command(4, 1, "I confirm the displayed Spec Package.")
    foundation.execute(transcript)
    values = _base(5, actor=PM_ID)
    values.update(
        command_type="CONFIRM_ARTIFACT",
        actor_authentication=c.VerbalSelfAssertion(
            authentication_method=c.ActorAuthenticationMethod.VERBAL_SELF_ASSERTION,
            assurance_level=c.AssuranceLevel.SELF_ASSERTED,
            actor_id=PM_ID,
            asserted_display_name="PM",
            claimed_role="Product Manager",
            assertion_transcript_event_id=transcript.transcript.event_id,
        ),
        binding=c.ArtifactConfirmationBinding(
            artifact_type="SPEC_PACKAGE",
            artifact_id=artifact_id,
            artifact_key="SPEC-TEST",
            artifact_version=1,
            record_revision=1,
            payload_hash=digest,
            confirmation_id=confirmation_id,
            view_id=view_id,
            view_hash="sha256:" + "a" * 64,
        ),
        confirmation_transcript_event_id=transcript.transcript.event_id,
        approved_exception_ids=(),
    )
    command = c.ConfirmArtifactCommand(**values)
    first = foundation.execute(command)
    assert foundation.execute(command) == first
    refreshed = WorkshopFoundationService(url, now=lambda: NOW).confirmed_artifact(
        CASE_ID, "SPEC_PACKAGE"
    )
    assert refreshed is not None
    assert refreshed["artifact_id"] == artifact_id
    assert refreshed["payload_hash"] == digest

    duplicate = command.model_copy(
        update={"command_id": uuid4(), "idempotency_key": f"idem-{uuid4()}"}
    )
    duplicate = duplicate.model_copy(update={"expected_case_revision": 6})
    with pytest.raises(FoundationProtocolError) as error:
        foundation.execute(duplicate)
    assert error.value.code is c.FoundationRejectionCode.CONFIRMATION_BINDING_FAILED
    with engine_for(url).connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(tables["workshop_artifact_confirmations"])
        ).scalar_one() == 1
        assert connection.execute(select(func.count()).select_from(audit_events)).scalar_one() == 6


def test_guidance_is_admitted_only_from_exact_foundation_question_and_dependencies(tmp_path):
    url, foundation = _runtime(tmp_path)
    _activate(foundation)
    brief = c.InterviewBriefCandidate(
        protocol_version="1.0.0",
        output_type="INTERVIEW_BRIEF_CANDIDATE",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        request_hash=REQUEST_HASH,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=4,
        customer_promise_summary="Export filtered orders.",
        evidence_candidates=(
            c.EvidenceCandidate(
                candidate_key="evidence-brief",
                source_role=c.SourceRole.TECHNICAL_CONTRACT,
                locator=c.SourceLineLocator(
                    locator_kind=c.SourceLocatorKind.SOURCE_LINES,
                    start_line=531,
                    end_line=531,
                ),
                relevance_claim="The source fixes encoding.",
                quoted_text_candidate="CSV output uses UTF-8 and one header row.",
            ),
        ),
        problems=(
            c.ProblemCandidate(
                candidate_key="problem-brief",
                problem_kind=c.ProblemKind.MISSING_DECISION,
                domain=c.Domain.PRODUCT,
                severity=c.Severity.HIGH,
                statement="The PM must confirm the encoding policy.",
                consequence="Consumers need a stable encoding.",
                evidence_candidate_keys=("evidence-brief",),
            ),
        ),
        problem_clusters=(),
        questions=(
            c.QuestionCandidate(
                candidate_key="question-brief",
                text="Should every export use UTF-8?",
                rationale="This closes the encoding decision.",
                question_shape=c.QuestionShape.CLOSED_BOOLEAN,
                capture_policy=c.CapturePolicy.BINDING_DECISION,
                answer_options=(),
                addresses_problem_keys=("problem-brief",),
                prerequisite_problem_keys=(),
                safe_without_current_turn_interpretation=True,
            ),
        ),
        initial_runway=c.QuestionRunwayCandidate(
            recommended_question_key="question-brief",
            safe_alternate_question_keys=(),
            do_not_ask_question_keys=(),
        ),
        confirmation_checkpoints=(),
    )
    values = _base(4)
    values.update(
        command_type="ADMIT_INTERVIEW_BRIEF",
        analyzer_run_id=RUN_ID,
        context_id=CONTEXT_ID,
        provider_request_hash=REQUEST_HASH,
        candidate=brief,
    )
    admitted = foundation.execute(c.AdmitInterviewBriefCommand(**values))
    question = next(item for item in admitted.identity_mappings if item.entity_kind == "QUESTION")
    problem = next(item for item in admitted.identity_mappings if item.entity_kind == "PROBLEM")
    question_ref = c.FoundationEntityRef(
        ref_kind="FOUNDATION_ID",
        foundation_id=question.foundation_id,
        expected_version=question.record_version,
    )
    guidance_candidate = c.GuidanceCandidate(
        protocol_version="1.0.0",
        output_type="GUIDANCE_CANDIDATE",
        analyzer_run_id=UUID("10000000-0000-4000-8000-000000000099"),
        context_id=CONTEXT_ID,
        request_hash="sha256:" + "9" * 64,
        source_set_hash=SOURCE_SET_HASH,
        based_on_case_revision=5,
        recommended_question=c.GuidanceQuestion(
            question_ref=question_ref,
            exact_text="Should every export use UTF-8?",
            reason="This is the next safe question.",
        ),
        safe_alternates=(),
        do_not_ask_question_refs=(),
        dependencies=(
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.SOURCE_SET,
                entity_ref=None,
            ),
            c.GuidanceDependency(
                dependency_kind=c.GuidanceDependencyKind.PROBLEM,
                entity_ref=c.FoundationEntityRef(
                    ref_kind="FOUNDATION_ID",
                    foundation_id=problem.foundation_id,
                    expected_version=problem.record_version,
                ),
            ),
        ),
        acknowledgement_suggestion="The encoding question is ready.",
    )
    guidance_values = _base(5)
    guidance_values.update(
        command_type="ADMIT_GUIDANCE",
        analyzer_run_id=guidance_candidate.analyzer_run_id,
        context_id=CONTEXT_ID,
        provider_request_hash=guidance_candidate.request_hash,
        candidate=guidance_candidate,
    )
    receipt = foundation.execute(c.AdmitGuidanceCommand(**guidance_values))
    assert receipt.admitted_guidance_id is not None
    with engine_for(url).connect() as connection:
        payload = connection.execute(
            select(WORKSHOP_PROTOCOL_TABLES["workshop_guidance"].c.payload_json).where(
                WORKSHOP_PROTOCOL_TABLES["workshop_guidance"].c.valid == 1
            )
        ).scalar_one()
    guidance = c.AdmittedGuidance.model_validate_json(payload)
    assert guidance.recommended_question.question_id == question.foundation_id
    assert c.GuidanceInvalidationTrigger.SOURCE_SET_CHANGED in guidance.invalidation_triggers
