from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update
from fastapi.testclient import TestClient

from specops_contracts import workshop_v1 as c
from specops_workflow import WorkflowService
from specops_workflow.persistence import (
    TASK28_RUNTIME_TABLES,
    V0_RUNTIME_TABLES,
    WORKSHOP_PROTOCOL_TABLES,
    source_artifacts,
)
from specops_workshop.chat_application import (
    FakeFutureVoiceContextConsumer,
    ParticipantTurnError,
    ParticipantTurnIngress,
    WorkshopConversationProjector,
    VisualProposalActionService,
)
from specops_workshop.chat_contracts import (
    ChatbotGuidanceRequest,
    ChatbotGuidanceSelection,
    ChatbotQuestionRef,
    CommittedParticipantTurn,
    InputChannel,
    SubmitTypedResponseIntent,
    VisualProposalActionIntent,
)
from specops_workshop.chatbot_provider import (
    ChatbotGuidanceCoordinator,
    OpenAIResponsesChatbotProvider,
)
from specops_workshop.api import create_app
from specops_workshop.sources import SourceCatalog
from specops_workshop.v4.orchestrator import V4ProductionOrchestrator
from specops_workshop.v4.scheduler import DurableAnalyzerWorker

from test_feature_25_v4_foundation import (
    CASE_ID,
    NOW,
    _activate,
    _runtime,
    _turn_candidate,
)
from test_feature_27_v0_preparation_runtime import _admit_brief
from test_feature_26_v4_production_seam import ROOT, DeterministicAdapter, configured


def _ready(tmp_path):
    _, foundation = _runtime(tmp_path)
    _activate(foundation)
    _admit_brief(foundation)
    foundation.set_preparation_phase(CASE_ID, "READY")
    case = foundation.get_case(CASE_ID)
    ingress = ParticipantTurnIngress(
        foundation,
        case_id=CASE_ID,
        session_id=case.session_id,
        actor_id=foundation.case_actor(CASE_ID, "PM"),
    )
    projector = WorkshopConversationProjector(
        foundation, case_id=CASE_ID, session_id=case.session_id
    )
    return foundation, ingress, projector


def _intent(foundation, *, text="  Exact café response.  ", correction=None):
    question = foundation.runway_projection(CASE_ID)["questions"][0]
    return SubmitTypedResponseIntent(
        client_submission_id=uuid4(),
        question_id=UUID(question["question_id"]),
        expected_question_version=question["question_version"],
        text=text,
        correction_of_response_id=correction,
        edit_target=None,
    )


def test_participant_turn_ingress_commits_every_authoritative_effect_atomically(tmp_path):
    foundation, ingress, projector = _ready(tmp_path)
    before_revision = foundation.case_revision(CASE_ID)
    before_depth = foundation.runway_projection(CASE_ID)["depth"]

    receipt = ingress.submit_typed(_intent(foundation))

    assert receipt.recovery_code == "ANALYSIS_QUEUED"
    assert receipt.snapshot.input_channel is InputChannel.CHAT
    assert receipt.snapshot.channel_confirmation_receipt_id is None
    assert receipt.snapshot.normalized_text == "Exact café response."
    assert foundation.case_revision(CASE_ID) == before_revision + 1
    assert foundation.runway_projection(CASE_ID)["depth"] == before_depth - 1
    with foundation.engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(source_artifacts).where(
                source_artifacts.c.case_id == str(CASE_ID),
                source_artifacts.c.type == "WORKSHOP_TRANSCRIPT",
            )
        ).scalar_one() == 1
        jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
        assert connection.execute(
            select(func.count()).select_from(jobs).where(
                jobs.c.subject_id == str(receipt.snapshot.response_id)
            )
        ).scalar_one() == 1
    context = projector.project()
    assert context.committed_turns[0].question.exact_text.startswith("Which confirmed")
    assert context.committed_turns[0].response == receipt.snapshot


def test_participant_turn_ingress_waits_for_prior_analysis_before_next_turn(tmp_path):
    foundation, ingress, projector = _ready(tmp_path)
    first_value = _intent(foundation)
    first = ingress.submit_typed(first_value)
    second_value = _intent(foundation, text="Use UTC for every export timestamp.")
    revision_while_pending = foundation.case_revision(CASE_ID)
    depth_while_pending = foundation.runway_projection(CASE_ID)["depth"]

    assert projector.project().turn_submission_status == "ANALYSIS_PENDING"
    assert ingress.submit_typed(first_value).replayed is True
    with pytest.raises(ParticipantTurnError, match="TURN_ANALYSIS_IN_PROGRESS"):
        ingress.submit_typed(second_value)
    assert foundation.case_revision(CASE_ID) == revision_while_pending
    assert foundation.runway_projection(CASE_ID)["depth"] == depth_while_pending

    jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
    with foundation.engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == str(first.analyzer_job_id))
            .values(state="FAILED")
        )
    assert projector.project().turn_submission_status == "ANALYSIS_FAILED"
    with pytest.raises(ParticipantTurnError, match="TURN_ANALYSIS_FAILED"):
        ingress.submit_typed(second_value)

    with foundation.engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == str(first.analyzer_job_id))
            .values(state="COMPLETED")
        )

    assert projector.project().turn_submission_status == "READY"
    second = ingress.submit_typed(second_value)
    assert second.snapshot.turn_sequence == 2
    with foundation.engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == str(second.analyzer_job_id))
            .values(state="COMPLETED")
        )
    assert projector.project().turn_submission_status == "ANALYSIS_PENDING"
    with pytest.raises(ParticipantTurnError, match="TURN_ANALYSIS_IN_PROGRESS"):
        ingress.submit_typed(_intent(foundation, text="Keep the header row stable."))

    with foundation.engine.begin() as connection:
        connection.execute(
            update(jobs)
            .where(jobs.c.operation == c.AnalyzerOperation.GUIDANCE.value)
            .values(state="COMPLETED")
        )
    assert projector.project().turn_submission_status == "READY"


def test_typed_turn_visual_confirm_binds_exact_transcript_for_synthesis(tmp_path):
    foundation, ingress, projector = _ready(tmp_path)
    turn = ingress.submit_typed(_intent(foundation, text="Use UTC for the export timestamp."))
    context = foundation.active_analyzer_context(CASE_ID)

    class DecisionAdapter(DeterministicAdapter):
        @staticmethod
        def source_set_hash(_sources):
            return foundation.get_case(CASE_ID).source_set_hash

        async def execute(self, request, *, context):
            candidate = _turn_candidate(
                request.transcript.transcript_event_id,
                request.based_on_case_revision,
            )
            return candidate.model_copy(
                update={
                    "analyzer_run_id": request.analyzer_run_id,
                    "context_id": request.context_id,
                    "request_hash": request.request_hash,
                    "source_set_hash": request.source_set_hash,
                }
            )

    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=DecisionAdapter(),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    worker = DurableAnalyzerWorker(orchestrator, worker_id="task28-visual-proposal-worker")

    assert asyncio.run(worker.run_once()) is True
    job = next(
        item
        for item in foundation.analyzer_jobs(CASE_ID)
        if item["job_id"] == str(turn.analyzer_job_id)
    )
    assert job["state"] == "COMPLETED"
    pending_decision_ids = {
        item.decision_id
        for item in foundation.semantic_snapshot(CASE_ID).decisions
        if item.status is c.SemanticRecordStatus.PENDING_CONFIRMATION
    }
    view = foundation.current_decision_view(CASE_ID)
    assert view is not None
    assert {item.pending_decision_id for item in view.items} == pending_decision_ids
    proposals = projector.project().proposal_statuses
    assert len(proposals) == 1
    assert proposals[0].status.value == "PENDING"
    assert proposals[0].view == view

    revision_after_materialization = foundation.case_revision(CASE_ID)
    worker._materialize_decision_review(
        c.ProposalAdmissionReceipt.model_validate_json(job["admission_receipt_json"])
    )
    assert foundation.case_revision(CASE_ID) == revision_after_materialization
    assert foundation.current_decision_view(CASE_ID) == view

    confirmed = VisualProposalActionService(projector, orchestrator).confirm(
        VisualProposalActionIntent(
            client_action_id=uuid4(), binding=proposals[0].binding
        )
    )
    assert confirmed.status.value == "COMMITTED"
    with foundation.engine.connect() as connection:
        protocol_events = WORKSHOP_PROTOCOL_TABLES["workshop_protocol_events"]
        assert connection.execute(
            select(func.count()).select_from(protocol_events).where(
                protocol_events.c.case_id == str(CASE_ID),
                protocol_events.c.event_type == "RECORD_FINAL_TRANSCRIPT_APPLIED",
            )
        ).scalar_one() == 0
    bindings = foundation.confirmed_decision_synthesis_bindings(CASE_ID)
    assert len(bindings) == len(pending_decision_ids)
    assert {item.transcript_event_id for item in bindings} == {
        turn.snapshot.response_id
    }


def test_ingress_idempotency_correction_and_channel_provenance_survive_restart(tmp_path):
    foundation, ingress, projector = _ready(tmp_path)
    value = _intent(foundation)
    first = ingress.submit_typed(value)
    replay = ingress.submit_typed(value)
    assert replay.replayed is True
    assert replay.snapshot == first.snapshot
    with pytest.raises(ParticipantTurnError, match="IDEMPOTENCY_CONFLICT"):
        ingress.submit_typed(value.model_copy(update={"text": "Different"}))

    with foundation.engine.begin() as connection:
        jobs = V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
        connection.execute(
            update(jobs)
            .where(jobs.c.job_id == str(first.analyzer_job_id))
            .values(state="COMPLETED")
        )
    correction = ingress.submit_typed(
        SubmitTypedResponseIntent(
            client_submission_id=uuid4(),
            question_id=first.snapshot.question_id,
            expected_question_version=first.snapshot.question_version,
            text="Corrected exact response.",
            correction_of_response_id=first.snapshot.response_id,
            edit_target=None,
        )
    )
    assert correction.snapshot.turn_sequence == first.snapshot.turn_sequence
    assert correction.snapshot.response_version == first.snapshot.response_version + 1
    assert projector.project().committed_turns == FakeFutureVoiceContextConsumer(
        projector
    ).activate().committed_turns
    restarted = WorkshopConversationProjector(
        type(foundation)(foundation.engine.url.render_as_string(hide_password=False), now=lambda: NOW),
        case_id=CASE_ID,
        session_id=foundation.get_case(CASE_ID).session_id,
    )
    assert restarted.project().committed_turns == projector.project().committed_turns
    legacy_restart = WorkflowService(
        database_url=foundation.engine.url.render_as_string(hide_password=False)
    )
    assert legacy_restart._cases[CASE_ID].revision == foundation.case_revision(CASE_ID)


def test_voice_confirmed_is_rejected_before_any_ingress_transaction(tmp_path):
    foundation, ingress, _ = _ready(tmp_path)
    question = foundation.runway_projection(CASE_ID)["questions"][0]
    revision = foundation.case_revision(CASE_ID)
    with pytest.raises(ParticipantTurnError, match="INPUT_CHANNEL_NOT_ENABLED"):
        ingress.commit(
            CommittedParticipantTurn(
                client_submission_id=uuid4(),
                question_id=UUID(question["question_id"]),
                expected_question_version=question["question_version"],
                normalized_text="Unconfirmed future transcript.",
                correction_of_response_id=None,
                edit_target=None,
                input_channel=InputChannel.VOICE_CONFIRMED,
                channel_confirmation_receipt_id=uuid4(),
            )
        )
    assert foundation.case_revision(CASE_ID) == revision
    with foundation.engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(
                TASK28_RUNTIME_TABLES["workshop_typed_responses"]
            )
        ).scalar_one() == 0


def test_ingress_failure_rolls_back_snapshot_evidence_consumption_and_job(tmp_path):
    foundation, ingress, _ = _ready(tmp_path)
    before_depth = foundation.runway_projection(CASE_ID)["depth"]
    original = foundation._advance_revision

    def fail_revision(*_):
        raise RuntimeError("injected atomic rollback")

    foundation._advance_revision = fail_revision
    try:
        with pytest.raises(RuntimeError, match="atomic rollback"):
            ingress.submit_typed(_intent(foundation))
    finally:
        foundation._advance_revision = original
    assert foundation.runway_projection(CASE_ID)["depth"] == before_depth
    with foundation.engine.connect() as connection:
        assert connection.execute(
            select(func.count()).select_from(
                TASK28_RUNTIME_TABLES["workshop_typed_responses"]
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(source_artifacts).where(
                source_artifacts.c.type == "WORKSHOP_TRANSCRIPT"
            )
        ).scalar_one() == 0
        assert connection.execute(
            select(func.count()).select_from(
                V0_RUNTIME_TABLES["workshop_analyzer_jobs"]
            )
        ).scalar_one() == 0


def test_openai_chatbot_provider_is_luna_medium_stateless_and_selection_only():
    captured = {}
    selection = ChatbotGuidanceSelection(
        recommended_question_ref={
            "question_id": uuid4(),
            "question_version": 1,
        },
        safe_alternate_refs=(),
        do_not_ask_question_refs=(),
        acknowledgement_suggestion="Thanks. The next admitted question is ready.",
    )

    class Responses:
        async def create(self, **arguments):
            captured.update(arguments)
            return SimpleNamespace(output_text=selection.model_dump_json())

    provider = OpenAIResponsesChatbotProvider(
        api_key="fixture", client=SimpleNamespace(responses=Responses())
    )
    request = ChatbotGuidanceRequest(
        request_identity="chatbot-guidance:fixture",
        session_id=uuid4(),
        case_revision=7,
        readiness=c.Readiness.NEEDS_CLARIFICATION,
        review_obligation=c.ReviewObligation.NONE,
        askable_question_refs=(selection.recommended_question_ref,),
        consumed_question_refs=(),
        latest_response_id=None,
        latest_question_ref=None,
    )
    assert asyncio.run(provider.select_guidance(request)) == selection
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["store"] is False
    serialized = captured["input"][1]["content"]
    assert "exact_text" not in serialized
    assert "source_ref" not in serialized.casefold()
    assert "provider_conversation" not in serialized


def test_luna_selection_is_materialized_from_exact_supplied_foundation_questions(tmp_path):
    foundation, _, _ = _ready(tmp_path)
    context = foundation.active_analyzer_context(CASE_ID)
    captured = {}

    class SelectionFixture:
        async def select_guidance(self, request):
            captured["request"] = request
            return ChatbotGuidanceSelection(
                recommended_question_ref=request.askable_question_refs[-1],
                safe_alternate_refs=request.askable_question_refs[:3],
                do_not_ask_question_refs=(),
                acknowledgement_suggestion="The next admitted question is ready.",
            )

    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=SimpleNamespace(source_set_hash=lambda _: foundation.get_case(CASE_ID).source_set_hash),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    request = DurableAnalyzerWorker(orchestrator)._build_request(
        {"operation": "GUIDANCE", "job_id": str(uuid4())}, context
    )
    coordinator = ChatbotGuidanceCoordinator(
        foundation,
        SelectionFixture(),
        case_id=CASE_ID,
        session_id=context.session_id,
    )
    candidate = asyncio.run(coordinator.execute(request))
    bounded = captured["request"]
    assert bounded.request_identity.startswith("chatbot-guidance:")
    assert bounded.request_identity != request.client_request_id
    assert not hasattr(bounded, "provider_conversation_id")
    snapshot = {
        (item.question_id, item.question_version): item
        for item in foundation.semantic_snapshot(CASE_ID).questions
    }
    selected = snapshot[
        (
            candidate.recommended_question.question_ref.foundation_id,
            candidate.recommended_question.question_ref.expected_version,
        )
    ]
    assert candidate.recommended_question.exact_text == selected.text
    assert candidate.recommended_question.reason == selected.rationale


def test_luna_unknown_and_duplicate_refs_fall_back_to_supplied_askable_questions(tmp_path):
    foundation, _, _ = _ready(tmp_path)
    context = foundation.active_analyzer_context(CASE_ID)
    captured = {}

    class SelectionFixture:
        async def select_guidance(self, request):
            captured["request"] = request
            supplied = request.askable_question_refs
            unknown = ChatbotQuestionRef(question_id=uuid4(), question_version=1)
            return ChatbotGuidanceSelection(
                recommended_question_ref=unknown,
                safe_alternate_refs=(supplied[1], supplied[1], supplied[2]),
                do_not_ask_question_refs=(unknown, supplied[1], supplied[3]),
                acknowledgement_suggestion="The next admitted question is ready.",
            )

    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=SimpleNamespace(
            source_set_hash=lambda _: foundation.get_case(CASE_ID).source_set_hash
        ),
        case_id=CASE_ID,
        session_id=context.session_id,
        sources=(),
        analyzer_contract=context.analyzer_contract,
        now=lambda: NOW,
    )
    request = DurableAnalyzerWorker(orchestrator)._build_request(
        {"operation": "GUIDANCE", "job_id": str(uuid4())}, context
    )
    candidate = asyncio.run(
        ChatbotGuidanceCoordinator(
            foundation,
            SelectionFixture(),
            case_id=CASE_ID,
            session_id=context.session_id,
        ).execute(request)
    )

    supplied = {
        (item.question_id, item.question_version)
        for item in captured["request"].askable_question_refs
    }
    selected = (
        candidate.recommended_question,
        *candidate.safe_alternates,
    )
    selected_keys = [
        (item.question_ref.foundation_id, item.question_ref.expected_version)
        for item in selected
    ]
    blocked_keys = [
        (item.foundation_id, item.expected_version)
        for item in candidate.do_not_ask_question_refs
    ]
    assert selected_keys == [
        (
            captured["request"].askable_question_refs[1].question_id,
            captured["request"].askable_question_refs[1].question_version,
        ),
        (
            captured["request"].askable_question_refs[2].question_id,
            captured["request"].askable_question_refs[2].question_version,
        ),
    ]
    assert blocked_keys == [
        (
            captured["request"].askable_question_refs[3].question_id,
            captured["request"].askable_question_refs[3].question_version,
        )
    ]
    assert set(selected_keys).issubset(supplied)
    assert set(blocked_keys).issubset(supplied)
    assert set(selected_keys).isdisjoint(blocked_keys)


def test_v0_runtime_exposes_only_chat_ingress_and_read_only_exact_playback(tmp_path):
    app = create_app(
        settings=configured(tmp_path),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
        analyzer_adapter=DeterministicAdapter(),
    )
    paths = {route.path for route in app.routes}
    assert "/ws/live" not in paths
    assert "/api/session/final-turn" not in paths
    assert not any(path.startswith("/api/v4/voice/") for path in paths)
    assert "/api/v4/foundation/commands" not in paths
    assert "/api/workshop/responses" in paths
    with TestClient(app) as client:
        for _ in range(200):
            if client.get("/api/workshop/preparation").json()["phase"] == "READY":
                break
            import time

            time.sleep(0.01)
        before = client.get("/api/workshop").json()
        question = before["question_runway"]["questions"][0]
        playback = client.post(
            "/api/workshop/playback",
            json={
                "question_id": question["question_id"],
                "question_version": question["question_version"],
                "exact_text": question["exact_text"],
            },
        )
        after_playback = client.get("/api/workshop").json()
        assert playback.status_code == 200
        assert playback.json()["exact_text"] == question["exact_text"]
        assert after_playback["case_revision"] == before["case_revision"]
        assert after_playback["committed_turns"] == before["committed_turns"]

        response = client.post(
            "/api/workshop/responses",
            json={
                "client_submission_id": str(uuid4()),
                "question_id": question["question_id"],
                "expected_question_version": question["question_version"],
                "text": "Use the organization timezone and UTF-8.",
                "correction_of_response_id": None,
                "edit_target": None,
            },
        )
        assert response.status_code == 200, response.text
        receipt = response.json()
        assert receipt["snapshot"]["input_channel"] == "CHAT"
        assert receipt["snapshot"]["channel_confirmation_receipt_id"] is None
        committed = client.get("/api/workshop").json()["committed_turns"]
        assert committed[0]["question"]["exact_text"] == question["exact_text"]


def test_visual_edit_and_reject_are_explicit_idempotent_proposal_actions(tmp_path):
    foundation, _, projector = _ready(tmp_path)
    view = c.DecisionBatchReviewView(
        protocol_version=c.PROTOCOL_VERSION,
        view_type="DECISION_BATCH_REVIEW",
        view_id=uuid4(),
        view_hash="sha256:" + "a" * 64,
        session_id=foundation.get_case(CASE_ID).session_id,
        based_on_case_revision=foundation.case_revision(CASE_ID),
        derived_from_cluster_ids=(),
        items=(
            c.DecisionReviewItemView(
                review_item_id=uuid4(),
                handle="A",
                pending_decision_id=uuid4(),
                pending_decision_version=1,
                classification=c.Domain.PRODUCT,
                exact_statement="Use UTF-8 for every exported CSV file.",
                rationale="The technical contract fixes the encoding.",
                problem_origins=(
                    c.ProblemOriginView(
                        problem_id=uuid4(),
                        problem_version=1,
                        problem_statement="CSV encoding must be fixed.",
                        resolution_kind=c.ProblemResolutionKind.FULL,
                        evidence_summary="The bound source specifies UTF-8.",
                    ),
                ),
            ),
        ),
        generated_at=NOW,
    )
    table = WORKSHOP_PROTOCOL_TABLES["workshop_decision_views"]
    with foundation.engine.begin() as connection:
        connection.execute(
            insert(table).values(
                view_id=str(view.view_id),
                case_id=str(CASE_ID),
                view_hash=view.view_hash,
                based_on_case_revision=view.based_on_case_revision,
                view_json=view.model_dump_json(),
                current=1,
                generated_at=NOW.isoformat().replace("+00:00", "Z"),
            )
        )
    proposal = projector.project().proposal_statuses[0]
    service = VisualProposalActionService(
        projector, SimpleNamespace(apply_review_selection=lambda **_: None)
    )
    edit_intent = VisualProposalActionIntent(
        client_action_id=uuid4(), binding=proposal.binding
    )
    edited = service.edit(edit_intent)
    assert edited.status.value == "EDIT_REQUESTED"
    assert service.edit(edit_intent).replayed is True
    rejected = service.reject(
        VisualProposalActionIntent(
            client_action_id=uuid4(), binding=proposal.binding
        )
    )
    assert rejected.status.value == "REJECTED"
    assert projector.project().proposal_statuses[0].status.value == "REJECTED"
