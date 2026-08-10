from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from specops_workflow import FrozenClock
from specops_workflow.models import AuditQuery, LineRange, QueryOne, SourceRef
from specops_workshop.analyzer import (
    AnalyzerProviderSchemaError,
    ControlIntent,
    GroundedSemanticText,
    SemanticFinding,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SupportingExcerpt,
)
from specops_workshop.api import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.native_schemas import (
    claude_semantic_turn_tool,
    local_semantic_turn_schema,
    openai_semantic_turn_schema,
)
from specops_workshop.ports import VoiceEvent, VoiceEventType
from specops_workshop.privacy_egress import TerraPrivacyEgressGateway
from specops_workshop.providers.openai_responses import TerraResponsesProvider
from specops_workshop.sources import SourceCatalog


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)


def configured(tmp_path: Path) -> Settings:
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-s2-c1-fixture",
        "OPENAI_API_KEY": "openai-s2-c1-fixture",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class AlwaysGrounded:
    def supports(self, _claim, _refs):
        return True


def package_draft(request) -> SemanticTurnDraft:
    candidate = request.candidates[0]

    def grounded(text: str) -> GroundedSemanticText:
        return GroundedSemanticText(
            text=text,
            evidence_aliases=(candidate.alias,),
            supporting_excerpts=(SupportingExcerpt(
                alias=candidate.alias, excerpt=candidate.text
            ),),
        )

    return SemanticTurnDraft(
        schema_version=1,
        outcome="PACKAGE_PROPOSAL",
        findings=(),
        package_delta=SemanticPackageDelta(
            item_title="Alias-sealed export decision",
            business_requirement=grounded("The selected agenda decision governs CSV export behavior."),
            technical_decision=grounded("Implement the selected technical agenda decision exactly."),
            acceptance_check=grounded("Verify the selected agenda behavior with an exact evidence link."),
        ),
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Proposal ready",
        next_question="Should we confirm this package?",
        uncertainty=None,
    )


def clarification_draft(request) -> SemanticTurnDraft:
    candidate = request.candidates[0]
    return SemanticTurnDraft(
        schema_version=1,
        outcome="FOCUSED_CLARIFICATION",
        findings=(SemanticFinding(
            domain="TECHNICAL",
            question=GroundedSemanticText(
                text="Which selected agenda decision should govern this package?",
                evidence_aliases=(candidate.alias,),
                supporting_excerpts=(SupportingExcerpt(
                    alias=candidate.alias, excerpt=candidate.text
                ),),
            ),
            uncertainty="The decision needs one explicit selection.",
        ),),
        package_delta=None,
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Decision needed",
        next_question="Which selected agenda decision should govern this package?",
        uncertainty="The decision needs one explicit selection.",
    )


def finish_control_draft(_request) -> SemanticTurnDraft:
    return SemanticTurnDraft(
        schema_version=1,
        outcome="CONTROL",
        findings=(),
        package_delta=None,
        control_intent=ControlIntent.FINISH,
        edit_instruction=None,
        acknowledgement="Audit complete",
        next_question=None,
        uncertainty=None,
    )


class FixtureAnalyzer:
    def __init__(self, factory=package_draft) -> None:
        self.factory = factory
        self.requests = []

    async def analyze(self, request):
        self.requests.append(request)
        return self.factory(request)


def make_app(tmp_path: Path, analyzer: FixtureAnalyzer, voice_provider=object()):
    return create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=voice_provider,
        analyzer_provider=analyzer,
        grounding_checker=AlwaysGrounded(),
    )


@pytest.mark.parametrize(
    ("turn", "alias", "line"),
    [
        ("Resolve D-01 before sign-off.", "technical-agenda:d-01", 527),
        ("Resolve D-02 timezone configuration.", "technical-agenda:d-02", 528),
        ("Resolve D-05 currency exponent rounding.", "technical-agenda:d-05", 531),
    ],
)
def test_s2_c1_productive_turn_endpoint_confirms_once_and_refreshes(
    tmp_path, turn, alias, line
):
    analyzer = FixtureAnalyzer()
    app = make_app(tmp_path, analyzer)
    with TestClient(app) as client:
        final = client.post("/api/session/final-turn", json={
            "turn_sequence": 1,
            "text": turn,
            "provider_request_id": f"s2-c1-{line}",
            "correction_of_version": None,
        })
        assert final.status_code == 200
        assert len(analyzer.requests) == 1
        request = analyzer.requests[0]
        assert request.required_outcome == "PACKAGE_PROPOSAL"
        assert tuple(value.alias for value in request.candidates) == (alias,)

        projection = client.get("/api/workshop").json()
        pending = projection["pending_proposal"]
        assert pending is not None and pending["record"]["status"] == "PENDING"
        result = pending["result"]["complete_package_proposal"]
        assert result is not None
        expected = SourceRef(
            artifact_id=next(
                value.artifact_id for value in app.state.evidence_index.bindings
                if value.alias == alias
            ),
            version=1,
            content_hash=next(
                value.content_hash for value in app.state.evidence_index.bindings
                if value.alias == alias
            ),
            location=LineRange(start=line, end=line),
        ).model_dump(mode="json")
        refs = [
            value["source_refs"][0]
            for value in (
                result["requirements"]
                + result["technical_decisions"]
                + result["acceptance_checks"]
            )
        ]
        assert refs == [expected, expected, expected]

        session = app.state.workshop_store.get_session(DEMO_SESSION_ID)
        before = app.state.workflow.list_audit_events(AuditQuery(
            case_id=session.case_id, acting_actor_id=session.pm_actor_id, limit=500
        )).items
        control = {
            "intent": "CONFIRM",
            "proposal_ref": pending["record"]["proposal_ref"],
            "edit_instruction": None,
            "acknowledgement": "Confirmed",
        }
        assert client.post("/api/proposals/control", json=control).json() == {"status": "COMMITTED"}
        committed = client.get("/api/workshop").json()
        assert committed["pending_proposal"] is None
        assert committed["governance"]["items"][0]["readiness"] == "READY"
        after = app.state.workflow.list_audit_events(AuditQuery(
            case_id=session.case_id, acting_actor_id=session.pm_actor_id, limit=500
        )).items
        assert len(after) > len(before)

        duplicate = client.post("/api/proposals/control", json=control)
        assert duplicate.status_code == 409
        refreshed = client.get("/api/workshop").json()
        assert refreshed["pending_proposal"] is None
        final_audit = app.state.workflow.list_audit_events(AuditQuery(
            case_id=session.case_id, acting_actor_id=session.pm_actor_id, limit=500
        )).items
        assert final_audit == after


def test_s2_c1_ambiguous_turn_is_local_focused_clarification_without_provider(tmp_path):
    analyzer = FixtureAnalyzer()
    app = make_app(tmp_path, analyzer)
    with TestClient(app) as client:
        response = client.post("/api/session/final-turn", json={
            "turn_sequence": 1,
            "text": "Should we settle the date or timezone decision?",
            "provider_request_id": "s2-c1-ambiguous",
            "correction_of_version": None,
        })
        assert response.status_code == 200
        assert analyzer.requests == []
        assert client.get("/api/workshop").json()["pending_proposal"] is None
        recovery = client.get("/api/analyzer/recovery").json()
        assert recovery["recovery_action"] == "CLARIFY"


def test_s2_c1_outcome_contract_rejects_none_or_multiple_branches(tmp_path):
    app = make_app(tmp_path, FixtureAnalyzer())
    unit = app.state.evidence_index.unit_for("technical-agenda:d-01")
    request = type("Request", (), {"candidates": (type("Candidate", (), {
        "alias": unit.alias, "text": unit.text,
    })(),)})()
    package = package_draft(request)
    clarification = clarification_draft(request)
    control = finish_control_draft(request)
    assert package.outcome == "PACKAGE_PROPOSAL"
    assert clarification.outcome == "FOCUSED_CLARIFICATION"
    assert control.outcome == "CONTROL"

    no_outcome = package.model_dump(mode="python")
    no_outcome.pop("outcome")
    with pytest.raises(ValidationError):
        SemanticTurnDraft.model_validate(no_outcome)
    multiple_outcomes = package.model_dump(mode="python")
    multiple_outcomes["findings"] = clarification.model_dump(mode="python")["findings"]
    with pytest.raises(ValidationError):
        SemanticTurnDraft.model_validate(multiple_outcomes)


def test_s2_c1_wrong_required_outcome_cannot_materialize_or_commit(tmp_path):
    analyzer = FixtureAnalyzer(clarification_draft)
    app = make_app(tmp_path, analyzer)
    with TestClient(app) as client:
        response = client.post("/api/session/final-turn", json={
            "turn_sequence": 1,
            "text": "Resolve D-01 before sign-off.",
            "provider_request_id": "s2-c1-wrong-outcome",
            "correction_of_version": None,
        })
        assert response.status_code == 200
        assert len(analyzer.requests) == 2
        assert client.get("/api/workshop").json()["pending_proposal"] is None
        query = QueryOne(
            case_id=app.state.bootstrap.case_id,
            acting_actor_id=app.state.bootstrap.pm_actor_id,
        )
        assert app.state.workflow.get_workflow_view(query).current_package is None


def test_s2_c1_explicit_control_turn_requires_the_control_branch(tmp_path):
    analyzer = FixtureAnalyzer(finish_control_draft)
    app = make_app(tmp_path, analyzer)
    with TestClient(app) as client:
        response = client.post("/api/session/final-turn", json={
            "turn_sequence": 1,
            "text": "Finish workshop after D-01 sign-off.",
            "provider_request_id": "s2-c1-control",
            "correction_of_version": None,
        })
        assert response.status_code == 200
        assert len(analyzer.requests) == 1
        assert analyzer.requests[0].required_outcome == "CONTROL"
        assert client.get("/api/workshop").json()["pending_proposal"] is None


def test_s2_c1_silent_noop_fails_closed_with_safe_validation_receipt(tmp_path):
    request_analyzer = FixtureAnalyzer()
    app = make_app(tmp_path, request_analyzer)
    unit = app.state.evidence_index.unit_for("technical-agenda:d-02")
    request = type("Request", (), {"candidates": (type("Candidate", (), {
        "alias": unit.alias, "text": unit.text,
    })(),)})()
    raw = package_draft(request).model_dump(mode="json")
    raw.pop("outcome")

    class Responses:
        async def create(self, **_kwargs):
            return type("Response", (), {"output_text": json.dumps(raw)})()

    provider = TerraResponsesProvider(
        api_key="server-only-s2-c1", model=TERRA_MODEL,
        client=type("Client", (), {"responses": Responses()})(),
    )
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id="s2-c1-noop",
    )
    # Use the real gate request shape without making an external call.
    gate = app.state.gate
    gate.analyzer = provider
    result = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    assert result.complete_package_proposal is None
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    query = QueryOne(
        case_id=app.state.bootstrap.case_id,
        acting_actor_id=app.state.bootstrap.pm_actor_id,
    )
    assert app.state.workflow.get_workflow_view(query).current_package is None
    receipt = provider.request_diagnostics[-1].as_receipt()
    assert receipt["validation_diagnostics"] == [{"path": ["outcome"], "code": "missing"}]
    serialized = json.dumps(receipt, sort_keys=True)
    assert unit.text not in serialized
    assert "Resolve D-02" not in serialized


def test_s2_c1_native_schemas_and_two_block_egress_are_identity_free(tmp_path):
    app = make_app(tmp_path, FixtureAnalyzer())
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-05 currency exponent rounding.",
        provider_request_id="s2-c1-schema",
    )
    asyncio.run(app.state.gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    request = app.state.gate.analyzer.requests[0]
    local_schema = local_semantic_turn_schema()
    openai_schema = openai_semantic_turn_schema()
    claude_tool = claude_semantic_turn_tool()
    assert local_schema["discriminator"]["propertyName"] == "outcome"
    assert len(local_schema["oneOf"]) == 3
    assert len(openai_schema["anyOf"]) == 3
    assert claude_tool["input_schema"] == local_schema
    for branch in openai_schema["$defs"].values():
        if branch.get("title", "").endswith("SemanticDraft"):
            assert branch["additionalProperties"] is False
            assert set(branch["required"]) == set(branch["properties"])
    assert claude_tool["name"] == "semantic_turn_draft_v1"

    blocks = TerraPrivacyEgressGateway().content_blocks(request)
    assert [block["text"].partition("\n")[0] for block in blocks] == [
        "INSTRUCTIONS_AND_PHASE", "SEMANTIC_ANALYZER_REQUEST"
    ]
    manifest = TerraPrivacyEgressGateway().manifest(blocks).model_dump(mode="json")
    assert [value["label"] for value in manifest["blocks"]] == [
        "INSTRUCTIONS_AND_PHASE", "SEMANTIC_ANALYZER_REQUEST"
    ]
    assert all(set(value) == {"label", "byte_count"} for value in manifest["blocks"])
    serialized = json.dumps(blocks, sort_keys=True)
    semantic_payload = json.loads(blocks[1]["text"].split("\n", 1)[1])
    assert semantic_payload["business_context"] == request.business_context
    assert semantic_payload["final_turn_text"] == request.final_turn_text
    assert semantic_payload["candidates"][0]["text"] == request.candidates[0].text
    pm_document = (ROOT / "docs/PM_Specs.md").read_text()
    technical_document = (ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md").read_text()
    assert pm_document not in serialized
    assert technical_document not in serialized
    assert str(app.state.bootstrap.case_id) not in serialized
    assert app.state.evidence_snapshots[0].content_hash not in serialized
    assert request.final_turn_text not in json.dumps(manifest, sort_keys=True)


def test_s2_c1_voice_final_uses_the_shared_final_turn_processor(tmp_path):
    class VoiceSession:
        def __init__(self):
            self.closed = False

        async def send_audio(self, _frame):
            pass

        async def send_text(self, _text):
            pass

        async def interrupt(self):
            pass

        async def events(self):
            yield VoiceEvent(
                VoiceEventType.INPUT_FINAL,
                text="Resolve D-01 before sign-off.",
                provider_request_id="s2-c1-voice-final",
            )
            while not self.closed:
                await asyncio.sleep(0.01)

        async def close(self):
            self.closed = True

    class VoiceProvider:
        def __init__(self):
            self.session = VoiceSession()

        async def connect(self, _context):
            return self.session

    app = make_app(tmp_path, FixtureAnalyzer(), voice_provider=VoiceProvider())
    calls = []
    original = app.state.final_turn_processor.process

    async def traced(*args, **kwargs):
        calls.append((args, kwargs))
        return await original(*args, **kwargs)

    app.state.final_turn_processor.process = traced
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as websocket:
            events = [websocket.receive_json() for _ in range(4)]
            assert any(event["type"] == "PROPOSAL_PENDING" for event in events)
            websocket.send_text('{"type":"END"}')
            assert websocket.receive_json()["state"] == "ENDED"
    assert len(calls) == 1
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is not None
