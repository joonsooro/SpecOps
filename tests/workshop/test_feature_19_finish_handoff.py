from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from specops_workflow import FrozenClock
from specops_workflow.enums import Domain, ItemReadiness
from specops_workflow.models import QueryOne, UUIDListQuery
from specops_workshop.analyzer import (
    ControlIntent,
    GroundedSemanticText,
    SemanticFinding,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SupportingExcerpt,
)
from specops_workshop.legacy_test_app import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import CallState, ConversationPhase, WorkshopState
from specops_workshop.delegation import DEV_LEAD_ACTOR_ID, PM_ACTOR_ID
from specops_workshop.sources import SourceCatalog
from specops_workshop.ports import VoiceEvent, VoiceEventType


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 15, tzinfo=timezone.utc)


def configured(tmp_path):
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-test-value", "OPENAI_API_KEY": "openai-test-value",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL, "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class Grounded:
    def supports(self, _claim, _refs): return True


class FinishAnalyzer:
    def __init__(self, *, blocked=False):
        self.blocked = blocked
        self.requests = []

    async def analyze(self, request):
        self.requests.append(request)
        candidate = request.candidates[0]

        def grounded(text):
            return GroundedSemanticText(
                text=text,
                evidence_aliases=(candidate.alias,),
                supporting_excerpts=(SupportingExcerpt(
                    alias=candidate.alias, excerpt=candidate.text
                ),),
            )

        if request.purpose == "FINISH_AUDIT":
            if self.blocked:
                return SemanticTurnDraft(
                    schema_version=1,
                    outcome="FOCUSED_CLARIFICATION",
                    findings=(SemanticFinding(
                        domain=Domain.CROSS_DOMAIN,
                        question=grounded("How is timezone configuration resolved?"),
                        uncertainty="The selected agenda decision remains open.",
                    ),),
                    package_delta=None,
                    control_intent=ControlIntent.NONE,
                    edit_instruction=None,
                    acknowledgement="Audit requires decision",
                    next_question="How is timezone configuration resolved?",
                    uncertainty="The selected agenda decision remains open.",
                )
            return SemanticTurnDraft(
                schema_version=1, outcome="CONTROL", findings=(), package_delta=None,
                control_intent=ControlIntent.FINISH, edit_instruction=None,
                acknowledgement="Audit complete", next_question=None, uncertainty=None,
            )
        if request.final_turn_text.lower().startswith("finish workshop"):
            return SemanticTurnDraft(
                schema_version=1, outcome="CONTROL", findings=(), package_delta=None,
                control_intent=ControlIntent.FINISH, edit_instruction=None,
                acknowledgement="Workshop finished", next_question=None, uncertainty=None,
            )
        return SemanticTurnDraft(
            schema_version=1,
            outcome="PACKAGE_PROPOSAL",
            findings=(),
            package_delta=SemanticPackageDelta(
                item_title="Complete filtered export",
                business_requirement=grounded("Use the organization timezone."),
                technical_decision=grounded("Use an IANA timezone with UTC fallback."),
                acceptance_check=grounded("Timezone boundaries and rendering stay exact."),
            ),
            control_intent=ControlIntent.NONE,
            edit_instruction=None,
            acknowledgement="Drafted grounded package",
            next_question="Should this package be committed?",
            uncertainty=None,
        )


def runtime(tmp_path, analyzer, live_provider=object()):
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT), live_provider=live_provider,
        analyzer_provider=analyzer, grounding_checker=Grounded(),
    )
    return app, TestClient(app)


def prepare(client):
    assert client.post("/api/session/final-turn", json={
        "turn_sequence": 1,
        "text": "Resolve D-02 timezone configuration for the export.",
        "provider_request_id": "finish-final-1",
        "correction_of_version": None,
    }).status_code == 200
    pending = client.get("/api/workshop").json()["pending_proposal"]
    assert client.post("/api/proposals/control", json={
        "intent": "CONFIRM", "proposal_ref": pending["record"]["proposal_ref"],
        "edit_instruction": None, "acknowledgement": "Confirmed",
    }).status_code == 200


def test_finish_runs_medium_audit_keeps_voice_live_and_exposes_exact_idempotent_handoff(tmp_path):
    analyzer = FinishAnalyzer()
    app, client = runtime(tmp_path, analyzer)
    with client:
        prepare(client)
        finished = client.post("/api/finish")
        assert finished.status_code == 200
        value = finished.json()
        assert value["workshop_state"] == "COMPLETED"
        assert value["conversation_phase"] == "HANDOFF_READY"
        handoff = value["handoff"]
        assert len(handoff["ready_item_bindings"]) == 1
        assert [value["item_id"] for value in handoff["ready_item_bindings"]] == sorted(
            value["item_id"] for value in handoff["ready_item_bindings"]
        )
        assert handoff["blocked_review_requests"] == []
        assert len(handoff["later_review_requests"]) == 1
        assert [value["review_request_id"] for value in handoff["later_review_requests"]] == sorted(
            value["review_request_id"] for value in handoff["later_review_requests"]
        )
        assert len(handoff["transcript_source_refs"]) == 1
        session = app.state.workshop_store.get_session(DEMO_SESSION_ID)
        assert session.workshop_state == WorkshopState.COMPLETED
        assert session.conversation_phase == ConversationPhase.HANDOFF_READY
        assert session.call_state == CallState.LISTENING
        projection = client.get("/api/workshop").json()
        assert projection["handoff"] == handoff
        assert client.post("/api/finish").json()["handoff"] == handoff
        assert [request.effort for request in analyzer.requests] == ["medium", "medium"]
        assert [request.purpose for request in analyzer.requests] == ["TURN", "FINISH_AUDIT"]
        assert client.post("/api/proposals/control", json={
            "intent": "REJECT", "proposal_ref": "old", "edit_instruction": None,
            "acknowledgement": None,
        }).status_code == 409


def test_pending_patch_prevents_finish_before_medium_audit(tmp_path):
    analyzer = FinishAnalyzer()
    _, client = runtime(tmp_path, analyzer)
    with client:
        assert client.post("/api/session/final-turn", json={
            "turn_sequence": 1, "text": "Resolve D-02 timezone configuration.",
            "provider_request_id": "pending-finish", "correction_of_version": None,
        }).status_code == 200
        refused = client.post("/api/finish")
        assert refused.status_code == 409
        assert "pending package proposal" in refused.json()["detail"]
        assert [request.effort for request in analyzer.requests] == ["medium"]


def test_explicit_technical_blocker_creates_native_decision_request_and_may_finish(tmp_path):
    analyzer = FinishAnalyzer(blocked=True)
    app, client = runtime(tmp_path, analyzer)
    with client:
        prepare(client)
        before = client.get("/api/workshop").json()["governance"]["items"][0]
        assert before["readiness"] == ItemReadiness.READY.value
        finished = client.post("/api/finish")
        assert finished.status_code == 200
        handoff = finished.json()["handoff"]
        assert handoff["ready_item_bindings"] == []
        assert len(handoff["blocked_review_requests"]) == 1
        assert [value["review_request_id"] for value in handoff["blocked_review_requests"]] == sorted(
            value["review_request_id"] for value in handoff["blocked_review_requests"]
        )
        assert len(handoff["later_review_requests"]) == 1
        assert {request["kind"] for request in handoff["blocked_review_requests"]} == {"DECISION_REQUIRED"}
        assert {request["question"] for request in handoff["blocked_review_requests"]} == {
            "How is timezone configuration resolved?",
        }
        assert {
            frozenset(request["reviewer_actor_ids"])
            for request in handoff["blocked_review_requests"]
        } == {frozenset((str(PM_ACTOR_ID), str(DEV_LEAD_ACTOR_ID)))}
        projection = client.get("/api/workshop").json()
        assert projection["governance"]["items"][0]["readiness"] == "BLOCKED"
        assert app.state.workshop_store.get_session(DEMO_SESSION_ID).conversation_phase == ConversationPhase.HANDOFF_READY


def test_semantic_finding_schema_cannot_supply_authoritative_owner():
    from pydantic import ValidationError
    import pytest

    with pytest.raises(ValidationError):
        SemanticFinding.model_validate({
            "domain": "TECHNICAL",
            "question": {
                "text": "Which timezone is authoritative?",
                "evidence_aliases": ["technical-agenda:d-02"],
                "supporting_excerpts": [{
                    "alias": "technical-agenda:d-02", "excerpt": "timezone"
                }],
            },
            "uncertainty": "The choice is open.",
            "owner_actor_ids": [str(DEV_LEAD_ACTOR_ID)],
        })


def test_provider_final_spoken_finish_uses_same_coordinator_and_does_not_end_voice(tmp_path):
    class VoiceSession:
        def __init__(self): self.closed = False; self.sent = []
        async def send_audio(self, _frame): pass
        async def send_text(self, value): self.sent.append(value)
        async def interrupt(self): pass
        async def events(self):
            yield VoiceEvent(
                VoiceEventType.INPUT_FINAL,
                text="finish workshop after D-02 timezone configuration",
                provider_request_id="spoken-finish-final",
            )
            while not self.closed: await asyncio.sleep(.01)
        async def close(self): self.closed = True
    class VoiceProvider:
        def __init__(self): self.session = VoiceSession()
        async def connect(self, _context): return self.session

    analyzer = FinishAnalyzer()
    voice = VoiceProvider()
    app, client = runtime(tmp_path, analyzer, voice)
    with client:
        prepare(client)
        with client.websocket_connect("/ws/live") as websocket:
            assert websocket.receive_json()["state"] == "CONNECTING"
            assert websocket.receive_json()["state"] == "LISTENING"
            assert websocket.receive_json()["type"] == "TRANSCRIPT_FINAL"
            finished = websocket.receive_json()
            assert finished["type"] == "FINISH_COMPLETE"
            assert len(finished["handoff"]["transcript_source_refs"]) == 2
            assert finished["handoff"]["transcript_source_refs"] == [
                snapshot.final_source_ref.model_dump(mode="json")
                for snapshot in app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)
            ]
            session = app.state.workshop_store.get_session(DEMO_SESSION_ID)
            assert session.conversation_phase == ConversationPhase.HANDOFF_READY
            assert session.call_state == CallState.LISTENING
            assert voice.session.closed is False
            assert voice.session.sent and voice.session.sent[-1].startswith("HANDOFF_READY")
            websocket.send_text('{"type":"END"}')
            assert websocket.receive_json()["state"] == "ENDED"
    assert [request.effort for request in analyzer.requests] == ["medium", "medium", "medium"]
    assert [request.purpose for request in analyzer.requests] == [
        "TURN", "TURN", "FINISH_AUDIT"
    ]


def test_reconnect_sends_only_authorized_exact_context_in_turn_version_order(tmp_path):
    class CaptureSession:
        def __init__(self): self.closed = False
        async def send_audio(self, _frame): pass
        async def send_text(self, _value): pass
        async def interrupt(self): pass
        async def events(self):
            while not self.closed: await asyncio.sleep(.01)
            if False:
                yield VoiceEvent(VoiceEventType.DISCONNECTED)
        async def close(self): self.closed = True

    class CaptureProvider:
        def __init__(self): self.contexts = []; self.session = CaptureSession()
        async def connect(self, context):
            self.contexts.append(context)
            return self.session

    analyzer = FinishAnalyzer()
    provider = CaptureProvider()
    app, client = runtime(tmp_path, analyzer, provider)
    with client:
        prepare(client)
        finished = client.post("/api/finish").json()
        app.state.coordinator.commit_final_turn(
            DEMO_SESSION_ID,
            turn_sequence=1,
            text="Keep every filtered row; preserve the exact fixed schema.",
            provider_request_id="finish-final-1-correction",
            correction_of_version=1,
        )
        with client.websocket_connect("/ws/live") as websocket:
            assert websocket.receive_json()["state"] == "CONNECTING"
            assert websocket.receive_json()["state"] == "LISTENING"
            websocket.send_text('{"type":"END"}')
            assert websocket.receive_json()["state"] == "ENDED"

        context = provider.contexts[-1]
        session = app.state.workshop_store.get_session(DEMO_SESSION_ID)
        query = QueryOne(case_id=session.case_id, acting_actor_id=session.pm_actor_id)
        assert context.resume.conversation_phase == ConversationPhase.HANDOFF_READY
        assert context.resume.committed_package == app.state.workflow.get_spec_package_content(query)
        assert context.resume.downstream_handoff == app.state.workflow.get_downstream_handoff(query)
        assert context.resume.downstream_handoff.model_dump(mode="json") != finished["handoff"]
        assert [(value.turn_sequence, value.version) for value in context.resume.final_transcript_snapshots] == [
            (1, 1), (1, 2)
        ]
        assert context.resume.final_transcript_snapshots == app.state.workshop_store.all_snapshots(DEMO_SESSION_ID)

        serialized = context.resume.model_dump_json()
        assert set(context.resume.model_dump()) == {
            "conversation_phase", "committed_package", "downstream_handoff", "final_transcript_snapshots"
        }
        assert "gemini-test-value" not in serialized
        assert "openai-test-value" not in serialized
        assert "raw_audio" not in serialized
        assert "PM_Specs.md" not in serialized
        assert "filtered-orders-csv-export-technical-spec.md" not in serialized
