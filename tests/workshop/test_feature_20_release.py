from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from specops_workflow import FrozenClock
from specops_workflow.enums import (
    ApprovalScope,
    ArtifactKind,
    Domain,
    ItemReadiness,
    PackageReadiness,
    ReviewObligation,
)
from specops_workflow.models import (
    ArtifactBinding,
    ItemBinding,
    LineRange,
    SourceRef,
    SpecPackageContentView,
    SpecPackageGovernanceView,
    SpecPackageItemView,
    SpecPackagePayloadV2,
    Requirement,
    TechnicalDecision,
    AcceptanceCheck,
    SpecPackageItem,
)
from specops_workshop.analyzer import (
    CommittedSemanticContext,
    ControlIntent,
    GroundedSemanticText,
    SelectedSemanticEvidence,
    SemanticAnalyzerRequest,
    SemanticPackageDelta,
    SemanticTurnDraft,
    SupportingExcerpt,
)
from specops_workshop.legacy_test_app import DEMO_SESSION_ID, create_app
from specops_workshop.boundary import assert_downstream_boundary
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import ConversationPhase
from specops_workshop.delegation import load_delegation_fixture
from specops_workshop.gate import RegisteredEvidenceGrounding
from specops_workshop.providers.openai_responses import (
    TerraProviderRequestError,
    TerraResponsesProvider,
)
from specops_workshop.ports import VoiceEvent, VoiceEventType
from specops_workshop.privacy_egress import TerraPrivacyEgressGateway
from specops_workshop.sessions import WorkshopStore
from specops_workshop.sources import SourceCatalog, SourceName
from specops_workshop.telemetry import (
    JsonTelemetry,
    LatencyMeasurement,
    LatencyMetric,
    LatencySpan,
    OperationalEvent,
    SpanOutcome,
    TelemetryStage,
    redact_operational_fields,
    release_latency_p95,
)
from sw_release_contract import SW_EVIDENCE, SW_IDS


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
BACKEND = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 9, 16, tzinfo=timezone.utc)


def configured(tmp_path: Path) -> Settings:
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-release-secret",
        "OPENAI_API_KEY": "openai-release-secret",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL,
        "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


def _legacy_authorized_terra_context_is_exact_and_excludes_contract(tmp_path, monkeypatch):
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    app.state.coordinator.commit_final_turn(
        DEMO_SESSION_ID,
        turn_sequence=1,
        text="Use the organization timezone for filtered export dates.",
        provider_request_id="release-provider-final-1",
    )
    snapshot = app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID)[0]
    plan = TerraDecisionPlan(
        schema_version=1,
        turn_source_ref=snapshot.final_source_ref,
        findings=[TerraFindingPlan(
            domain=Domain.TECHNICAL,
            evidence_refs=[snapshot.final_source_ref],
            clarification_question="Which export decision remains unresolved?",
        )],
    )
    outline = TerraPackageOutline(
        schema_version=1,
        turn_source_ref=snapshot.final_source_ref,
        item_title="Filtered orders CSV export",
        business_requirement=TerraGroundedText(
            statement="Export all orders matching the committed filters.",
            evidence_refs=[snapshot.final_source_ref],
        ),
        technical_decision=TerraGroundedText(
            statement="Generate large filtered exports asynchronously.",
            evidence_refs=[snapshot.final_source_ref],
        ),
        acceptance_check=TerraGroundedText(
            statement="The export contains every matching order.",
            evidence_refs=[snapshot.final_source_ref],
        ),
    )
    responses = [
        TerraCacheWarmResult(status="ready"),
        plan,
        TerraCacheWarmResult(status="ready"),
        outline,
    ]

    class Responses:
        def __init__(self): self.calls = []
        async def create(self, **kwargs):
            self.calls.append(kwargs)
            value = responses[len(self.calls) - 1]
            return type("Response", (), {
                "output_text": value.model_dump_json(),
                "_request_id": f"req_test_{len(self.calls)}",
            })()

    client = type("Client", (), {"responses": Responses()})()
    request = AnalyzerRequest(
        request_id=uuid4(),
        session_id=DEMO_SESSION_ID,
        effort="low",
        phase=ConversationPhase.WORKSHOP,
        final_turn=snapshot,
        source_context=app.state.analyzer_source_context,
        final_transcript_snapshots=(snapshot,),
        committed_context_json='{"content":null,"edit_instruction":null,"governance":null}',
    )
    provider = TerraResponsesProvider(
        api_key="server-only-release-secret",
        model=TERRA_MODEL,
        client=client,
    )
    analyzed = asyncio.run(provider.analyze(request))
    assert analyzed.acknowledgement == "Evidence analyzed"
    assert analyzed.next_question == plan.findings[0].clarification_question
    assert analyzed.complete_package_proposal is not None
    assert len(client.responses.calls) == 4
    warm_call, call, outline_warm_call, outline_call = client.responses.calls
    warm_blocks = warm_call["input"][0]["content"]
    assert [block["text"].partition("\n")[0] for block in warm_blocks] == [
        "INSTRUCTIONS_AND_PHASE",
        "AUTHORITY_METADATA",
        "PM_SPECS_DOCUMENT",
        "DEV_LEAD_TECHNICAL_SPEC_DOCUMENT",
        "CACHE_WARMUP_CONTEXT",
    ]
    warm_serialized = json.dumps(warm_blocks, sort_keys=True)
    assert "COMMITTED_PACKAGE_STATE" not in warm_serialized
    assert "FINAL_TRANSCRIPT_EVIDENCE" not in warm_serialized
    assert snapshot.normalized_text not in warm_serialized
    message = call["input"][0]
    blocks = message["content"]
    context = request.source_context
    assert context is not None
    assert message["role"] == "user"
    assert len(blocks) == 7
    assert all(block["type"] == "input_text" for block in blocks)
    assert set(blocks[3]) == {"type", "text", "prompt_cache_breakpoint"}
    assert blocks[3]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert all(
        "prompt_cache_breakpoint" not in block
        for index, block in enumerate(blocks)
        if index != 3
    )
    labels = [block["text"].partition("\n")[0] for block in blocks]
    assert labels == [
        "INSTRUCTIONS_AND_PHASE",
        "AUTHORITY_METADATA",
        "PM_SPECS_DOCUMENT",
        "DEV_LEAD_TECHNICAL_SPEC_DOCUMENT",
        "COMMITTED_PACKAGE_STATE",
        "FINAL_TRANSCRIPT_EVIDENCE",
        "CURRENT_FINAL_TURN",
    ]
    payloads = {
        label: json.loads(block["text"].partition("\n")[2])
        for label, block in zip(labels[1:], blocks[1:], strict=True)
    }
    assert [value.source_name for value in context.documents] == [
        "PM_SPECS", "DEV_LEAD_TECHNICAL_SPEC"
    ]
    assert context.documents[0].content == (ROOT / "docs/PM_Specs.md").read_text()
    assert context.documents[1].content == (
        ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md"
    ).read_text()
    assert context.authority.delegation_domain == "TECHNICAL"
    assert context.authority.later_review_required is True
    assert blocks[0]["text"].endswith("Phase: WORKSHOP")
    assert set(payloads["AUTHORITY_METADATA"]) == {"actors", "technical_delegation"}
    assert [value["role"] for value in payloads["AUTHORITY_METADATA"]["actors"]] == [
        "PM", "DEV_LEAD"
    ]
    assert payloads["AUTHORITY_METADATA"]["technical_delegation"] == {
        "active": True,
        "command_scope": list(context.authority.delegation_command_scope),
        "delegate_role": "PM",
        "delegator_role": "DEV_LEAD",
        "domain": "TECHNICAL",
        "later_review_required": True,
    }
    expected_document_fields = {
        "source_name", "artifact_id", "version", "content_hash", "content"
    }
    assert set(payloads["PM_SPECS_DOCUMENT"]) == expected_document_fields
    assert set(payloads["DEV_LEAD_TECHNICAL_SPEC_DOCUMENT"]) == expected_document_fields
    assert payloads["PM_SPECS_DOCUMENT"]["content"] == context.documents[0].content
    assert payloads["DEV_LEAD_TECHNICAL_SPEC_DOCUMENT"]["content"] == (
        context.documents[1].content
    )
    assert payloads["COMMITTED_PACKAGE_STATE"] == {
        "acceptance_checks": [],
        "edit_instruction": None,
        "existing_package_id": None,
        "items": [],
        "package_readiness": None,
        "requirements": [],
        "technical_decisions": [],
    }
    assert payloads["FINAL_TRANSCRIPT_EVIDENCE"] == [{
        "corrects_version": None,
        "source_ref": snapshot.final_source_ref.model_dump(mode="json"),
        "text": snapshot.normalized_text,
        "turn_sequence": snapshot.turn_sequence,
        "version": snapshot.version,
    }]
    assert payloads["CURRENT_FINAL_TURN"] == {
        "turn_sequence": snapshot.turn_sequence,
        "version": snapshot.version,
    }
    assert call["store"] is False
    assert call["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
    assert warm_call["prompt_cache_key"] == (
        "specops-workshop-terra-v0-source-decision-plan"
    )
    assert call["prompt_cache_key"] == warm_call["prompt_cache_key"]
    assert outline_warm_call["prompt_cache_key"] == (
        "specops-workshop-terra-v0-source-package-outline"
    )
    assert outline_call["prompt_cache_key"] == outline_warm_call["prompt_cache_key"]
    client_ids = [
        value["extra_headers"]["X-Client-Request-Id"]
        for value in (warm_call, call, outline_warm_call, outline_call)
    ]
    assert len(set(client_ids)) == 4
    assert client_ids[0].startswith("specops-workshop-cache_warm-")
    assert client_ids[1].startswith("specops-workshop-analysis-")
    assert client_ids[2].startswith("specops-workshop-cache_warm-")
    assert client_ids[3].startswith("specops-workshop-analysis-")
    assert [
        value["text"]["format"]["name"]
        for value in (warm_call, call, outline_warm_call, outline_call)
    ] == [
        "terra_cache_warm_v1",
        "terra_decision_plan_v1",
        "terra_cache_warm_v1",
        "terra_package_outline_v1",
    ]
    assert [value.as_receipt() for value in provider.request_diagnostics] == [
        {
            "stage": "CACHE_WARM", "outcome": "PASS",
            "client_request_id": client_ids[0], "provider_request_id": "req_test_1",
            "status_code": 200, "error_type": None,
        },
        {
            "stage": "ANALYSIS", "outcome": "PASS",
            "client_request_id": client_ids[1], "provider_request_id": "req_test_2",
            "status_code": 200, "error_type": None,
        },
        {
            "stage": "CACHE_WARM", "outcome": "PASS",
            "client_request_id": client_ids[2], "provider_request_id": "req_test_3",
            "status_code": 200, "error_type": None,
        },
        {
            "stage": "ANALYSIS", "outcome": "PASS",
            "client_request_id": client_ids[3], "provider_request_id": "req_test_4",
            "status_code": 200, "error_type": None,
        },
    ]
    gateway = TerraPrivacyEgressGateway()
    manifest_json = gateway.manifest(tuple(blocks)).model_dump_json()
    assert set(json.loads(manifest_json)) == {"destination", "policy_version", "blocks"}
    assert snapshot.normalized_text not in manifest_json
    assert context.documents[0].content not in manifest_json
    assert context.documents[1].content not in manifest_json
    authority = payloads["AUTHORITY_METADATA"]
    assert "case_id" not in authority
    assert "technical_delegation_id" not in authority
    assert "delegation_valid_from" not in authority
    assert "delegation_valid_until" not in authority
    assert "canonical_locator" not in payloads["PM_SPECS_DOCUMENT"]
    assert "media_type" not in payloads["PM_SPECS_DOCUMENT"]
    assert "session_id" not in payloads["FINAL_TRANSCRIPT_EVIDENCE"][0]
    assert "provider_request_id" not in payloads["FINAL_TRANSCRIPT_EVIDENCE"][0]
    assert "created_at" not in payloads["FINAL_TRANSCRIPT_EVIDENCE"][0]
    assert "stable_artifact_id" not in payloads["FINAL_TRANSCRIPT_EVIDENCE"][0]
    serialized_blocks = json.dumps(blocks, sort_keys=True)
    assert "filtered-orders-csv-export-technical-contract.md" not in serialized_blocks
    assert "server-only-release-secret" not in serialized_blocks
    assert "downstream_handoff" not in serialized_blocks
    assert '"reviews"' not in serialized_blocks

    with pytest.raises(ValueError, match="closed V0 gateway contract"):
        gateway.project(request.model_copy(update={
            "committed_context_json": (
                '{"content":null,"edit_instruction":null,"governance":null,"foundation_revision":7}'
            )
        }))

    class SlowResponses:
        async def create(self, **_kwargs):
            await asyncio.sleep(1)

    slow_client = type("SlowClient", (), {"responses": SlowResponses()})()
    import specops_workshop.providers.openai_responses as terra_module
    monkeypatch.setattr(terra_module, "TERRA_REQUEST_TIMEOUT_SECONDS", 0.01)
    slow_provider = TerraResponsesProvider(
        api_key="server-only-release-secret",
        model=TERRA_MODEL,
        client=slow_client,
    )
    with pytest.raises(TerraProviderRequestError) as captured:
        asyncio.run(slow_provider.analyze(request))
    assert captured.value.stage == "CACHE_WARM"
    assert isinstance(captured.value.__cause__, TimeoutError)
    assert len(captured.value.request_diagnostics) == 1
    assert slow_provider.request_diagnostics[-1].as_receipt() == {
        "stage": "CACHE_WARM",
        "outcome": "FAIL",
        "client_request_id": captured.value.client_request_id,
        "provider_request_id": None,
        "status_code": None,
        "error_type": "TimeoutError",
    }

    class AnalysisFailure(RuntimeError):
        request_id = "req_analysis_520"
        status_code = 520

    class AnalysisFailureResponses:
        def __init__(self): self.calls = 0
        async def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return type("Response", (), {
                    "output_text": TerraCacheWarmResult(status="ready").model_dump_json(),
                    "_request_id": "req_cache_warm_pass",
                })()
            raise AnalysisFailure("provider content stays redacted")

    failed_provider = TerraResponsesProvider(
        api_key="server-only-release-secret",
        model=TERRA_MODEL,
        client=type("Client", (), {"responses": AnalysisFailureResponses()})(),
    )
    with pytest.raises(TerraProviderRequestError) as captured:
        asyncio.run(failed_provider.analyze(request))
    assert captured.value.stage == "ANALYSIS"
    assert captured.value.provider_request_id == "req_analysis_520"
    assert captured.value.status_code == 520
    assert len(captured.value.request_diagnostics) == 2
    assert [value.stage for value in failed_provider.request_diagnostics] == [
        "CACHE_WARM", "ANALYSIS"
    ]
    assert [value.outcome for value in failed_provider.request_diagnostics] == [
        "PASS", "FAIL"
    ]


def test_authorized_terra_context_is_exact_and_excludes_contract(tmp_path, monkeypatch):
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
            alias=unit.alias,
            display_label=unit.display_label,
            text=unit.text,
        ),),
        committed_context=CommittedSemanticContext(package=None),
        remaining_budget_ms=25_000,
    )
    support = GroundedSemanticText(
        text="Use the organization timezone.",
        evidence_aliases=(unit.alias,),
        supporting_excerpts=(SupportingExcerpt(alias=unit.alias, excerpt=unit.text),),
    )
    draft = SemanticTurnDraft(
        schema_version=1,
        outcome="PACKAGE_PROPOSAL",
        findings=(),
        package_delta=SemanticPackageDelta(
            item_title="Timezone-safe export",
            business_requirement=support,
            technical_decision=support.model_copy(
                update={"text": "Use the organization profile IANA zone."}
            ),
            acceptance_check=support.model_copy(
                update={"text": "Timezone boundaries and rendering stay exact."}
            ),
        ),
        control_intent=ControlIntent.NONE,
        edit_instruction=None,
        acknowledgement="Evidence analyzed",
        next_question="Should we confirm this package?",
        uncertainty=None,
    )

    class Responses:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return type("Response", (), {
                "output_text": draft.model_dump_json(),
                "_request_id": "req_alias_semantic_1",
            })()

    client = type("Client", (), {"responses": Responses()})()
    provider = TerraResponsesProvider(
        api_key="server-only-release-secret", model=TERRA_MODEL, client=client
    )
    assert asyncio.run(provider.analyze(request)) == draft
    assert len(client.responses.calls) == 1
    call = client.responses.calls[0]
    assert call["model"] == TERRA_MODEL
    assert call["reasoning"] == {"effort": "medium"}
    assert call["store"] is False
    assert call["text"]["format"]["name"] == "semantic_turn_draft_v1"
    assert call["text"]["format"]["strict"] is True
    blocks = call["input"][0]["content"]
    assert [value["text"].partition("\n")[0] for value in blocks] == [
        "INSTRUCTIONS_AND_PHASE", "SEMANTIC_ANALYZER_REQUEST"
    ]
    serialized = json.dumps(blocks, sort_keys=True)
    provider_payload = json.loads(blocks[1]["text"].partition("\n")[2])
    assert provider_payload["business_context"] == request.business_context
    assert provider_payload["candidates"][0]["text"] == unit.text
    technical = (ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md").read_text()
    assert technical not in serialized
    assert "filtered-orders-csv-export-technical-contract.md" not in serialized
    assert "server-only-release-secret" not in serialized
    assert all(
        forbidden not in serialized
        for forbidden in (
            "artifact_id", "case_id", "actor_id", "package_id", "item_id",
            "proposal_id", "command_id", "content_hash", "source_ref", "line_range",
        )
    )
    receipt = provider.request_diagnostics[0].as_receipt()
    assert receipt["stage"] == "ANALYSIS" and receipt["outcome"] == "PASS"
    manifest = TerraPrivacyEgressGateway().manifest(tuple(blocks)).model_dump_json()
    assert request.final_turn_text not in manifest
    assert request.business_context not in manifest

    class SlowResponses:
        async def create(self, **_kwargs):
            await asyncio.sleep(1)

    import specops_workshop.providers.openai_responses as terra_module
    monkeypatch.setattr(terra_module, "TERRA_REQUEST_TIMEOUT_SECONDS", 0.01)
    slow = TerraResponsesProvider(
        api_key="server-only-release-secret",
        model=TERRA_MODEL,
        client=type("Client", (), {"responses": SlowResponses()})(),
    )
    with pytest.raises(TerraProviderRequestError) as captured:
        asyncio.run(slow.analyze(request))
    assert captured.value.stage == "ANALYSIS"
    assert len(captured.value.request_diagnostics) == 1


def test_operational_telemetry_redacts_and_secrets_stay_opaque(tmp_path):
    settings = configured(tmp_path)
    rendered = repr(settings) + settings.model_dump_json()
    assert "gemini-release-secret" not in rendered
    assert "openai-release-secret" not in rendered
    redacted = redact_operational_fields({
        "api_key": "secret",
        "transcript": "private words",
        "source_content": "private source",
        "raw_audio": b"pcm",
        "duration_ms": 42,
    })
    assert redacted == {
        "api_key": "[REDACTED]",
        "transcript": "[REDACTED]",
        "source_content": "[REDACTED]",
        "raw_audio": "[REDACTED]",
        "duration_ms": 42,
    }

    stream = StringIO()
    logger = logging.getLogger(f"specops.release.{uuid4()}")
    logger.handlers = [logging.StreamHandler(stream)]
    logger.setLevel(logging.INFO)
    event = OperationalEvent(
        event="latency_span.completed",
        stage=TelemetryStage.ANALYZER,
        session_id=uuid4(),
        span_id=uuid4(),
        duration_ms=42,
        outcome=SpanOutcome.OK,
        error_code=None,
    )
    JsonTelemetry(logger).emit(event)
    payload = json.loads(stream.getvalue())
    assert set(payload) == {
        "duration_ms", "error_code", "event", "outcome", "session_id", "span_id", "stage"
    }
    with pytest.raises(ValidationError):
        OperationalEvent.model_validate({**event.model_dump(mode="python"), "prompt": "forbidden"})


def test_terra_gateway_minimizes_committed_governance_identifiers(tmp_path):
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
            alias=unit.alias,
            display_label=unit.display_label,
            text=unit.text,
        ),),
        committed_context=CommittedSemanticContext(package=None),
        edit_instruction="Keep the current semantic item.",
        remaining_budget_ms=25_000,
    )
    payload = request.model_dump(mode="json")
    schema = SemanticAnalyzerRequest.model_json_schema()
    forbidden_keys = {
        "actor_id", "artifact_id", "binding", "case_id", "command_id",
        "content_hash", "item_id", "json_pointer", "line_range", "location",
        "package_id", "proposal_id", "raw_location", "source_ref",
        "source_version", "unit_id", "version", "workbook_location",
    }

    def object_keys(value):
        if isinstance(value, dict):
            yield from value
            for child in value.values():
                yield from object_keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from object_keys(child)

    assert forbidden_keys.isdisjoint(object_keys(payload))
    assert forbidden_keys.isdisjoint(object_keys(schema))
    serialized = json.dumps(payload, sort_keys=True)
    assert str(app.state.bootstrap.case_id) not in serialized
    assert app.state.evidence_snapshots[0].content_hash not in serialized


def test_semantic_grounding_rejects_an_unrelated_conclusion(tmp_path):
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    catalog = app.state.source_catalog
    lines = tuple(line for _, line in catalog.numbered_lines(SourceName.TECHNICAL_SPEC))
    ref = SourceRef(
        artifact_id=app.state.bootstrap.technical_source_id,
        version=1,
        content_hash=catalog.digest(SourceName.TECHNICAL_SPEC),
        location=LineRange(start=44, end=44),
    )
    checker = RegisteredEvidenceGrounding(
        static_refs={ref.artifact_id: (1, ref.content_hash, lines)},
        store=app.state.workshop_store,
        session_id=DEMO_SESSION_ID,
    )
    assert checker.supports("Retain the export file for 24 hours.", (ref,))
    assert not checker.supports("Quantum encryption is mandatory.", (ref,))


def test_five_stage_spans_and_thirty_turn_latency_bar(tmp_path):
    store = WorkshopStore(f"sqlite:///{tmp_path / 'spans.sqlite'}")
    session_id = uuid4()
    for index, stage in enumerate(TelemetryStage, start=1):
        started = NOW + timedelta(seconds=index)
        span = LatencySpan(
            span_id=uuid4(),
            session_id=session_id,
            stage=stage,
            started_at=started,
            ended_at=started + timedelta(milliseconds=25),
            duration_ms=25,
            outcome=SpanOutcome.OK,
        )
        assert store.record_completed_latency_span(span) == span
    assert {span.stage for span in store.list_latency_spans(session_id)} == set(TelemetryStage)

    targets = {
        LatencyMetric.PARTIAL_DISPLAY: 300,
        LatencyMetric.FINAL_DISPLAY: 700,
        LatencyMetric.FIRST_AGENT_AUDIO: 2200,
        LatencyMetric.BARGE_IN_STOP: 180,
    }
    measurements = tuple(
        LatencyMeasurement(turn_sequence=turn, metric=metric, duration_ms=value)
        for turn in range(1, 31)
        for metric, value in targets.items()
    )
    assert release_latency_p95(measurements) == targets
    with pytest.raises(ValueError, match="requires at least 30"):
        release_latency_p95(measurements[:-1])


def test_interruption_marker_is_idempotent_and_recoverable(tmp_path):
    url = f"sqlite:///{tmp_path / 'interruptions.sqlite'}"
    session_id = uuid4()
    store = WorkshopStore(url)
    values = {
        "session_id": session_id,
        "agent_turn_id": "agent-turn-7",
        "provider_request_id": "gemini-request-7",
        "interrupted_at": NOW,
    }
    store.record_agent_interruption(**values)
    store.record_agent_interruption(**values)
    restarted = WorkshopStore(url)
    assert restarted.list_agent_interruptions(session_id) == ({
        **values,
    },)


def test_midstream_provider_and_device_failure_keep_text_fallback(tmp_path):
    class Session:
        def __init__(self, midstream: bool): self.midstream = midstream; self.closed = False
        async def send_audio(self, _frame): pass
        async def send_text(self, _text): pass
        async def interrupt(self): pass
        async def events(self):
            if self.midstream:
                yield VoiceEvent(VoiceEventType.INPUT_PARTIAL, text="display only")
                yield VoiceEvent(VoiceEventType.DISCONNECTED)
                return
            while not self.closed:
                await asyncio.sleep(0.01)
        async def close(self): self.closed = True

    class Provider:
        def __init__(self, midstream: bool): self.session = Session(midstream)
        async def connect(self, _context): return self.session

    for index, midstream in enumerate((True, False)):
        runtime_root = tmp_path / str(index)
        runtime_root.mkdir()
        app = create_app(
            settings=configured(runtime_root),
            clock=FrozenClock(NOW),
            source_catalog=SourceCatalog(ROOT),
            live_provider=Provider(midstream),
        )
        with TestClient(app).websocket_connect("/ws/live") as websocket:
            assert websocket.receive_json()["state"] == "CONNECTING"
            assert websocket.receive_json()["state"] == "LISTENING"
            if midstream:
                assert websocket.receive_json() == {
                    "type": "TRANSCRIPT_PARTIAL", "text": "display only"
                }
            else:
                websocket.send_json({"type": "DEVICE_FAILURE"})
            disconnected = websocket.receive_json()
            assert disconnected["state"] == "DISCONNECTED"
            assert "guidance" in disconnected
            websocket.send_json({
                "type": "TEXT",
                "turn_sequence": 1,
                "text": "Keep every filtered row.",
                "provider_request_id": f"fallback-{index}",
            })
            response = websocket.receive_json()
            if response["type"] == "CALL_STATE":
                response = websocket.receive_json()
            assert response["type"] == "FINAL_COMMITTED"
            websocket.send_json({"type": "END"})
            assert websocket.receive_json()["state"] == "ENDED"


def test_delegation_identity_scope_and_frontmatter_fail_closed(tmp_path):
    technical_source = ROOT / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md"
    fixture_source = ROOT / "fixtures/dev-lead-delegation-email.md"
    for index, mutation in enumerate((
        lambda value: value.replace(
            "delegator_actor_id: 11111111-1111-4111-8111-111111111111",
            "delegator_actor_id: 33333333-3333-4333-8333-333333333333",
        ),
        lambda value: value.replace(
            "delegate_actor_id: 22222222-2222-4222-8222-222222222222",
            "delegate_actor_id: 44444444-4444-4444-8444-444444444444",
        ),
        lambda value: value.replace("  - revise_spec_package\n", ""),
        lambda value: value.removeprefix("---\n"),
    )):
        root = tmp_path / str(index)
        (root / "docs/technical-specs").mkdir(parents=True)
        (root / "fixtures").mkdir()
        technical = root / "docs/technical-specs/filtered-orders-csv-export-technical-spec.md"
        technical.write_bytes(technical_source.read_bytes())
        fixture = fixture_source.read_text().replace(str(technical_source), str(technical))
        (root / "fixtures/dev-lead-delegation-email.md").write_text(mutation(fixture))
        with pytest.raises(ValueError):
            load_delegation_fixture(SourceCatalog(root), now=NOW)


def test_downstream_boundary_has_no_platform_runtime_surface(tmp_path):
    assert_downstream_boundary(BACKEND)
    with pytest.raises(ValueError, match="outside the Workshop boundary"):
        Settings.load({
            "SPECOPS_DATABASE_URL": "sqlite:///:memory:",
            "WORKSHOP_DATABASE_URL": "sqlite:///:memory:",
            "GEMINI_API_KEY": "x",
            "OPENAI_API_KEY": "y",
            "JIRA_TOKEN": "forbidden",
        })
    app = create_app(
        settings=configured(tmp_path),
        clock=FrozenClock(NOW),
        source_catalog=SourceCatalog(ROOT),
        live_provider=object(),
    )
    paths = {route.path.casefold() for route in app.routes}
    assert not any("jira" in path or "github" in path for path in paths)
    runtime_files = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (BACKEND / "src/specops_workshop").rglob("*.py")
    ).casefold()
    assert "api.github.com" not in runtime_files
    assert "atlassian.net" not in runtime_files


def test_sw_release_inventory_is_closed_and_every_target_exists():
    assert tuple(SW_EVIDENCE) == SW_IDS
    allowed_kinds = {"pytest", "vitest", "playwright", "live"}
    for sw_id, rows in SW_EVIDENCE.items():
        assert rows, sw_id
        for kind, relative_path, evidence_name in rows:
            assert kind in allowed_kinds
            path = BACKEND / relative_path
            assert path.is_file(), f"{sw_id}: missing {relative_path}"
            if kind in {"pytest", "vitest", "playwright"}:
                assert evidence_name in path.read_text(encoding="utf-8"), (
                    f"{sw_id}: missing executable evidence {evidence_name}"
                )
