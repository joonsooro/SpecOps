from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from specops_workflow import FrozenClock
from specops_workflow.enums import Domain, ItemReadiness
from specops_workflow.models import LineRange, QueryOne, SourceRef
from specops_workshop.analyzer import (
    AcceptanceCheckProposal, AnalyzerTurnResult, CommittedSemanticContext,
    CompletePackageProposal, ControlIntent, ControlTarget, GroundedSemanticText,
    RequirementProposal, SelectedSemanticEvidence, SemanticAnalyzerRequest,
    SemanticPackageDelta, SemanticTurnDraft, SpecPackageItemProposal, SupportingExcerpt,
    TechnicalDecisionProposal, validate_phase,
)
from specops_workshop.legacy_test_app import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import (
    AnalyzerFailureKind,
    ConversationPhase,
    ProposalStatus,
)
from specops_workshop.gate import WorkshopGate
from specops_workshop.gate import RegisteredEvidenceGrounding
from specops_workshop.providers.openai_responses import TerraResponsesProvider
from specops_workshop.sources import SourceCatalog, SourceName


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 13, tzinfo=timezone.utc)


def configured(tmp_path):
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-test-value", "OPENAI_API_KEY": "openai-test-value",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL, "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class AnalyzerFixture:
    def __init__(self, result, failures=0): self.result=result; self.failures=failures; self.requests=[]
    async def analyze(self, request):
        self.requests.append(request)
        if len(self.requests) <= self.failures: raise ConnectionError("fixture analyzer unavailable")
        return self.result(request) if callable(self.result) else self.result
class GroundingFixture:
    def __init__(self, supported=True): self.supported=supported; self.calls=[]
    def supports(self, claim, refs): self.calls.append((claim, refs)); return self.supported


class DynamicAnalyzer:
    def __init__(self): self.requests = []
    async def analyze(self, request):
        self.requests.append(request)
        return semantic_draft(request)


class QuietVoiceSession:
    def __init__(self): self.text = []; self.interruptions = 0; self.closed = False
    async def send_audio(self, _frame): pass
    async def send_text(self, value): self.text.append(value)
    async def interrupt(self): self.interruptions += 1
    async def events(self):
        while not self.closed: await asyncio.sleep(0.01)
        if False: yield None
    async def close(self): self.closed = True


class QuietVoiceProvider:
    def __init__(self): self.session = QuietVoiceSession()
    async def connect(self, _context): return self.session


def proposal_result(ref):
    package = CompletePackageProposal(
        proposal_key="csv-package", existing_package_id=None,
        requirements=[RequirementProposal(proposal_key="all-rows", existing_unit_id=None, statement="Export every filtered order", domain=Domain.BUSINESS, delivery_required=True, source_refs=[ref])],
        technical_decisions=[TechnicalDecisionProposal(proposal_key="async-mode", existing_unit_id=None, statement="Generate large exports asynchronously", domain=Domain.TECHNICAL, delivery_required=True, source_refs=[ref])],
        acceptance_checks=[AcceptanceCheckProposal(proposal_key="complete-check", existing_check_id=None, statement="No filtered order is truncated", domain=Domain.CROSS_DOMAIN, related_unit_proposal_keys=["all-rows", "async-mode"], source_refs=[ref])],
        items=[SpecPackageItemProposal(proposal_key="complete-export", existing_item_id=None, title="Complete filtered export", requirement_proposal_keys=["all-rows"], technical_decision_proposal_keys=["async-mode"], acceptance_check_proposal_keys=["complete-check"], dependency_item_proposal_keys=[])],
    )
    return AnalyzerTurnResult(
        schema_version=1, turn_source_ref=ref, finding_proposals=[], complete_package_proposal=package,
        control_intent=ControlIntent.NONE, control_target=None, target_proposal_ref=None,
        edit_instruction=None, acknowledgement="Drafted grounded package", next_question="Should we confirm this package?",
    )


def semantic_draft(request):
    candidate = request.candidates[0]
    support = lambda text: GroundedSemanticText(
        text=text,
        evidence_aliases=(candidate.alias,),
        supporting_excerpts=(SupportingExcerpt(alias=candidate.alias, excerpt=candidate.text),),
    )
    return SemanticTurnDraft(
        schema_version=1,
        outcome="PACKAGE_PROPOSAL",
        findings=(),
        package_delta=SemanticPackageDelta(
            item_title="Timezone-safe filtered export",
            business_requirement=support("Use the organization profile timezone for exports."),
            technical_decision=support("Use the organization profile IANA zone with UTC fallback."),
            acceptance_check=support("Timezone boundaries and rendering use the selected configuration."),
        ),
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Drafted grounded package",
        next_question="Should we confirm this package?",
        uncertainty=None,
    )


def direct_gate(app, analyzer, grounding, *, foundation=None):
    return WorkshopGate(
        app.state.workshop_store,
        foundation or app.state.workflow,
        analyzer,
        grounding,
        clock=FrozenClock(NOW),
        evidence_index=app.state.evidence_index,
        evidence_snapshots=app.state.evidence_snapshots,
        business_context=app.state.analyzer_business_context,
        dev_lead_actor_id=app.state.bootstrap.dev_lead_actor_id,
    )


def test_final_evidence_proposes_without_mutation_then_confirm_commits_exact_cross_domain_item(tmp_path):
    app = create_app(settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(ROOT), live_provider=object())
    coordinator, store, foundation = app.state.coordinator, app.state.workshop_store, app.state.workflow
    coordinator.commit_final_turn(DEMO_SESSION_ID, turn_sequence=1, text="Resolve D-02 timezone configuration for the export.", provider_request_id="pm-final-1")
    snapshot = store.latest_snapshots(DEMO_SESSION_ID)[0]
    analyzer = AnalyzerFixture(semantic_draft)
    grounding = GroundingFixture(True)
    gate = direct_gate(app, analyzer, grounding)
    before = foundation.get_workflow_view(QueryOne(case_id=app.state.bootstrap.case_id, acting_actor_id=app.state.bootstrap.pm_actor_id))
    analyzed = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    after_analysis = foundation.get_workflow_view(QueryOne(case_id=before.case_id, acting_actor_id=app.state.bootstrap.pm_actor_id))
    assert analyzed.complete_package_proposal is not None
    assert after_analysis.revision == before.revision and after_analysis.current_package is None
    assert len(analyzer.requests) == 1
    assert analyzer.requests[0].effort == "medium"
    pending = store.pending_proposal(DEMO_SESSION_ID)
    assert pending is not None and pending.status == ProposalStatus.PENDING

    confirm = AnalyzerTurnResult(
        schema_version=1, turn_source_ref=snapshot.final_source_ref, finding_proposals=[], complete_package_proposal=None,
        control_intent=ControlIntent.CONFIRM, control_target=ControlTarget.WORKSHOP_PATCH,
        target_proposal_ref=pending.proposal_ref, edit_instruction=None, acknowledgement="Confirmed", next_question=None,
    )
    committed = gate.apply_control(DEMO_SESSION_ID, confirm, confirmation_context=True)
    assert committed.status == ProposalStatus.COMMITTED
    governance = foundation.get_spec_package_governance(QueryOne(case_id=before.case_id, acting_actor_id=app.state.bootstrap.pm_actor_id))
    assert governance.items[0].readiness == ItemReadiness.READY
    assert {scope.value for scope in governance.items[0].approval_scopes} == {"BUSINESS", "TECHNICAL"}
    assert store.get_session(DEMO_SESSION_ID).expected_foundation_revision == foundation.get_workflow_view(QueryOne(case_id=before.case_id, acting_actor_id=app.state.bootstrap.pm_actor_id)).revision


def test_ungrounded_or_wrong_phase_output_creates_no_proposal_or_foundation_mutation(tmp_path):
    app = create_app(settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(ROOT), live_provider=object())
    app.state.coordinator.commit_final_turn(DEMO_SESSION_ID, turn_sequence=1, text="Resolve D-02 timezone configuration.", provider_request_id="pm-final-2")
    gate = direct_gate(app, AnalyzerFixture(semantic_draft), GroundingFixture(False))
    revision = app.state.workflow.get_workflow_view(QueryOne(case_id=app.state.bootstrap.case_id, acting_actor_id=app.state.bootstrap.pm_actor_id)).revision
    recovered = asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    assert recovered.complete_package_proposal is None
    assert gate.latest_recovery(DEMO_SESSION_ID).failure_kind == AnalyzerFailureKind.GROUNDING
    assert app.state.workshop_store.pending_proposal(DEMO_SESSION_ID) is None
    assert app.state.workflow.get_workflow_view(QueryOne(case_id=app.state.bootstrap.case_id, acting_actor_id=app.state.bootstrap.pm_actor_id)).revision == revision
    with pytest.raises(ValueError, match="HANDOFF_READY"):
        validate_phase(proposal_result(app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)[0].final_source_ref).model_copy(update={"control_intent": ControlIntent.FINISH}), ConversationPhase.HANDOFF_READY)


def test_control_acknowledgement_question_and_extra_fields_are_strict():
    with pytest.raises(ValidationError):
        AnalyzerTurnResult.model_validate({"schema_version": 1, "unexpected": True})
    base = {
        "schema_version": 1,
        "turn_source_ref": SourceRef(
            artifact_id=UUID("00000000-0000-4000-8000-000000000001"),
            version=1,
            content_hash="1" * 64,
            location=LineRange(start=1, end=1),
        ),
        "finding_proposals": [],
        "complete_package_proposal": None,
        "control_intent": ControlIntent.NONE,
        "control_target": None,
        "target_proposal_ref": None,
        "edit_instruction": None,
    }
    with pytest.raises(ValidationError, match="five words"):
        AnalyzerTurnResult.model_validate({**base, "acknowledgement":"one two three four five six", "next_question":None})
    with pytest.raises(ValidationError, match="focused interrogative"):
        AnalyzerTurnResult.model_validate({**base, "acknowledgement":None, "next_question":"This is not a question."})


def test_missing_fields_reused_keys_and_model_uuid_are_rejected(tmp_path):
    with pytest.raises(ValidationError):
        AnalyzerTurnResult.model_validate({
            "schema_version": 1,
            "turn_source_ref": (base_ref := SourceRef(
                artifact_id=UUID("00000000-0000-4000-8000-000000000001"),
                version=1,
                content_hash="1" * 64,
                location=LineRange(start=1, end=1),
            )),
        })
    package = proposal_result(base_ref).complete_package_proposal
    assert package is not None
    with pytest.raises(ValidationError, match="globally unique"):
        CompletePackageProposal.model_validate({
            **package.model_dump(mode="python"),
            "items": [package.items[0].model_copy(update={"proposal_key": "all-rows"})],
        })

    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    with pytest.raises(ValidationError):
        SemanticTurnDraft.model_validate({
            **semantic_draft(SemanticAnalyzerRequest(
                schema_version=1,
                phase=ConversationPhase.WORKSHOP,
                final_turn_text="Resolve D-02.",
                business_context="Authorized business context",
                candidates=(SelectedSemanticEvidence(
                    alias="technical-agenda:d-02",
                    display_label="D-02 — Timezone configuration",
                    text="Timezone configuration uses an IANA zone.",
                ),),
                committed_context=CommittedSemanticContext(package=None),
                remaining_budget_ms=25_000,
            )).model_dump(mode="python"),
            "existing_package_id": uuid4(),
        })


def test_registered_evidence_rejects_unsupported_identity_hash_or_range(tmp_path):
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    catalog = app.state.source_catalog
    artifact_id = app.state.bootstrap.technical_source_id
    digest = catalog.digest(SourceName.TECHNICAL_SPEC)
    line_count = len(catalog.numbered_lines(SourceName.TECHNICAL_SPEC))
    checker = RegisteredEvidenceGrounding(
        static_refs={artifact_id: (1, digest, line_count)},
        store=app.state.workshop_store,
        session_id=DEMO_SESSION_ID,
    )
    valid = SourceRef(
        artifact_id=artifact_id,
        version=1,
        content_hash=digest,
        location=LineRange(start=1, end=1),
    )
    assert checker.supports("A grounded claim", (valid,))
    assert not checker.supports(
        "Unsupported range",
        (valid.model_copy(update={"location": LineRange(start=line_count + 1, end=line_count + 1)}),),
    )
    assert not checker.supports(
        "Unknown identity",
        (valid.model_copy(update={"artifact_id": uuid4()}),),
    )


def test_confirmation_requires_explicit_prompt_context_and_generic_ack_is_not_a_commit(tmp_path):
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id="generic-ack",
    )
    snapshot = app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)[0]
    gate = direct_gate(app, AnalyzerFixture(semantic_draft), GroundingFixture(True))
    asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)
    generic = AnalyzerTurnResult(
        schema_version=1,
        turn_source_ref=snapshot.final_source_ref,
        finding_proposals=[],
        complete_package_proposal=None,
        control_intent=ControlIntent.CONFIRM,
        control_target=ControlTarget.WORKSHOP_PATCH,
        target_proposal_ref=pending.proposal_ref,
        edit_instruction=None,
        acknowledgement="Okay",
        next_question=None,
    )
    before = app.state.workshop_store.get_session(DEMO_SESSION_ID).expected_foundation_revision
    with pytest.raises(ValueError, match="explicit"):
        gate.apply_control(DEMO_SESSION_ID, generic)
    assert app.state.workshop_store.get_session(DEMO_SESSION_ID).expected_foundation_revision == before
    wrong_target = generic.model_copy(update={"target_proposal_ref": "workshop-patch-wrong-v1"})
    with pytest.raises(ValueError, match="single visible"):
        gate.apply_control(DEMO_SESSION_ID, wrong_target, confirmation_context=True)


def test_substantive_voice_continuation_waits_for_typed_confirmed_foundation_commit(tmp_path):
    voice = QuietVoiceProvider()
    analyzer = DynamicAnalyzer()
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=voice,
        analyzer_provider=analyzer,
        grounding_checker=GroundingFixture(True),
    )
    with TestClient(app).websocket_connect("/ws/live") as websocket:
        assert websocket.receive_json()["state"] == "CONNECTING"
        assert websocket.receive_json()["state"] == "LISTENING"
        websocket.send_json({
            "type": "TEXT",
            "text": "Resolve D-02 timezone configuration for filtered exports.",
            "turn_sequence": 1,
            "provider_request_id": "progression-gate-final",
        })
        assert websocket.receive_json()["type"] == "FINAL_COMMITTED"
        pending = websocket.receive_json()
        assert pending["type"] == "PROPOSAL_PENDING"
        assert voice.session.text == []
        assert voice.session.interruptions == 1
        assert app.state.workflow.get_workflow_view(QueryOne(
            case_id=app.state.bootstrap.case_id,
            acting_actor_id=app.state.bootstrap.pm_actor_id,
        )).current_package is None

        websocket.send_json({
            "type": "CONTROL",
            "intent": "CONFIRM",
            "proposal_ref": pending["proposal_ref"],
            "acknowledgement": "Confirmed",
        })
        applied = websocket.receive_json()
        assert applied == {
            "type": "CONTROL_APPLIED",
            "intent": "CONFIRM",
            "status": "COMMITTED",
        }
        assert voice.session.text == [
            "The package commit is confirmed. Speak exactly this governed question and add nothing: "
            "Should we confirm this package?"
        ]
        governance = app.state.workflow.get_spec_package_governance(QueryOne(
            case_id=app.state.bootstrap.case_id,
            acting_actor_id=app.state.bootstrap.pm_actor_id,
        ))
        assert governance.items[0].readiness == ItemReadiness.READY
        websocket.send_json({"type": "END"})
        assert websocket.receive_json()["state"] == "ENDED"


def test_terra_adapter_pins_one_responses_call_and_medium_effort(tmp_path):
    class Responses:
        def __init__(self): self.calls = []
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return type("Response", (), {"output_text": response.model_dump_json()})()
    class Client:
        def __init__(self): self.responses = Responses()

    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    unit = app.state.evidence_index.unit_for("technical-agenda:d-02")
    request = SemanticAnalyzerRequest(
        schema_version=1,
        phase=ConversationPhase.WORKSHOP,
        final_turn_text="Resolve D-02 timezone configuration.",
        business_context=app.state.analyzer_business_context,
        candidates=(SelectedSemanticEvidence(
            alias=unit.alias, display_label=unit.display_label, text=unit.text
        ),),
        committed_context=CommittedSemanticContext(package=None),
        remaining_budget_ms=25_000,
    )
    response = semantic_draft(request)
    client = Client()
    provider = TerraResponsesProvider(
        api_key="server-only-test-value",
        model=TERRA_MODEL,
        client=client,
    )
    analyzed = asyncio.run(provider.analyze(request))
    assert analyzed == response
    assert len(client.responses.calls) == 1
    assert client.responses.calls[0]["text"]["format"]["name"] == "semantic_turn_draft_v1"
    for call in client.responses.calls:
        assert call["model"] == TERRA_MODEL
        assert call["reasoning"] == {"effort": "medium"}
        assert call["store"] is False
        assert call["text"]["format"]["type"] == "json_schema"
        assert call["text"]["format"]["strict"] is True
        assert "server-only-test-value" not in str(call)


def test_terra_medium_finish_audit_allows_no_new_finding_or_package(tmp_path):
    class Responses:
        def __init__(self): self.calls = []
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return type("Response", (), {"output_text": audit.model_dump_json()})()

    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    unit = app.state.evidence_index.unit_for("technical-agenda:d-02")
    audit = SemanticTurnDraft(
        schema_version=1,
        outcome="CONTROL", findings=(), package_delta=None, control_intent=ControlIntent.FINISH,
        edit_instruction=None, acknowledgement="Audit complete", next_question=None,
        uncertainty=None,
    )
    client = type("Client", (), {"responses": Responses()})()
    request = SemanticAnalyzerRequest(
        schema_version=1,
        purpose="FINISH_AUDIT",
        phase=ConversationPhase.WORKSHOP,
        required_outcome="CONTROL",
        final_turn_text="Resolve D-02 timezone configuration.",
        business_context=app.state.analyzer_business_context,
        candidates=(SelectedSemanticEvidence(
            alias=unit.alias, display_label=unit.display_label, text=unit.text
        ),),
        committed_context=CommittedSemanticContext(package=None),
        remaining_budget_ms=25_000,
    )
    result = asyncio.run(TerraResponsesProvider(
        api_key="server-only-test-value",
        model=TERRA_MODEL,
        client=client,
    ).analyze(request))
    assert result.package_delta is None
    assert result.findings == ()
    assert result.acknowledgement == "Audit complete"
    assert result.next_question is None
    assert [call["text"]["format"]["name"] for call in client.responses.calls] == [
        "semantic_turn_draft_v1"
    ]
    assert all(
        call["reasoning"] == {"effort": "medium"}
        for call in client.responses.calls
    )


def test_crash_after_package_commit_replays_same_outbox_identity_and_finishes_gate(tmp_path):
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Resolve D-02 timezone configuration.",
        provider_request_id="gate-crash-final",
    )
    snapshot = app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)[0]
    analyzer = AnalyzerFixture(semantic_draft)
    gate = direct_gate(app, analyzer, GroundingFixture(True))
    asyncio.run(gate.analyze_final_turn(DEMO_SESSION_ID, 1))
    pending = app.state.workshop_store.pending_proposal(DEMO_SESSION_ID)
    confirm = AnalyzerTurnResult(
        schema_version=1,
        turn_source_ref=snapshot.final_source_ref,
        finding_proposals=[],
        complete_package_proposal=None,
        control_intent=ControlIntent.CONFIRM,
        control_target=ControlTarget.WORKSHOP_PATCH,
        target_proposal_ref=pending.proposal_ref,
        edit_instruction=None,
        acknowledgement="Confirmed",
        next_question=None,
    )

    class CrashAfterCommit:
        def __init__(self, foundation): self.foundation = foundation; self.crashed = False
        def __getattr__(self, name): return getattr(self.foundation, name)
        def create_spec_package(self, command):
            result = self.foundation.create_spec_package(command)
            if not self.crashed:
                self.crashed = True
                raise ConnectionError("lost package response")
            return result

    crashing = direct_gate(
        app, analyzer, GroundingFixture(True),
        foundation=CrashAfterCommit(app.state.workflow),
    )
    with pytest.raises(ConnectionError, match="lost package"):
        crashing.apply_control(DEMO_SESSION_ID, confirm, confirmation_context=True)
    unknown = app.state.workshop_store.replayable_outbox(DEMO_SESSION_ID)
    assert len(unknown) == 1 and unknown[0].status.value == "UNKNOWN"
    committed_binding = app.state.workflow.get_workflow_view(QueryOne(
        case_id=app.state.bootstrap.case_id,
        acting_actor_id=app.state.bootstrap.pm_actor_id,
    )).current_package
    assert committed_binding is not None

    replay = direct_gate(app, analyzer, GroundingFixture(True)).apply_control(
        DEMO_SESSION_ID, confirm, confirmation_context=True
    )
    assert replay.status == ProposalStatus.COMMITTED
    assert app.state.workshop_store.replayable_outbox(DEMO_SESSION_ID) == ()
    assert app.state.workflow.get_workflow_view(QueryOne(
        case_id=app.state.bootstrap.case_id,
        acting_actor_id=app.state.bootstrap.pm_actor_id,
    )).current_package == committed_binding
