from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from specops_workflow import SystemClock
from specops_workflow.errors import DomainError
from specops_workflow.models import QueryOne, UUIDListQuery

from .analyzer import AnalyzerTurnResult, ControlIntent, ControlTarget
from .bootstrap import BootstrapView, bootstrap_foundation, stable_id
from .boundary import assert_downstream_boundary
from .config import Settings
from .contracts import FinalTurnInput, RecoveryView
from .delegation import load_delegation_fixture
from .evidence import (
    EvidenceIndexer,
    EvidenceSourceRole,
    RegisteredMarkdownSnapshot,
)
from .gate import RegisteredEvidenceGrounding, WorkshopGate
from .finish import FinishCoordinator, FinishResult
from .final_turn import FinalTurnProcessor
from .live_transport import LiveTransport
from .orchestration import WorkshopCoordinator
from .ports import LiveVoiceProvider
from .provider_context import build_analyzer_business_context
from .providers import GeminiLiveProvider
from .providers.openai_responses import TerraResponsesProvider
from .projections import PendingProposalView, ProposalControlInput, WorkshopProjection
from .sessions import WorkshopStore
from .sources import SourceCatalog, SourceName
from .telemetry import BrowserSpanInput, LatencySpan, TelemetryRecorder


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SPEC_ENG_ROOT = BACKEND_ROOT.parent / "Spec_Eng"
DEMO_SESSION_ID = stable_id("csv-export-workshop:session")


def create_app(
    *,
    settings: Settings | None = None,
    clock=None,
    source_catalog: SourceCatalog | None = None,
    live_provider: LiveVoiceProvider | None = None,
    analyzer_provider=None,
    grounding_checker=None,
    idle_timeout_seconds: float = 30 * 60,
) -> FastAPI:
    runtime_clock = clock or SystemClock()
    assert_downstream_boundary(BACKEND_ROOT)
    runtime_settings = settings or Settings.load(
        os.environ,
        Path(os.environ.get("SPECOPS_ENV_FILE", SPEC_ENG_ROOT / ".env")),
    )
    catalog = source_catalog or SourceCatalog(SPEC_ENG_ROOT)
    fixture = load_delegation_fixture(catalog, now=runtime_clock.now())
    workflow, bootstrap = bootstrap_foundation(
        runtime_settings,
        catalog,
        fixture,
        clock=runtime_clock,
    )
    workshop_store = WorkshopStore(runtime_settings.workshop_database_url)
    telemetry = TelemetryRecorder(workshop_store, clock=runtime_clock)
    coordinator = WorkshopCoordinator(
        workshop_store, workflow, clock=runtime_clock, telemetry=telemetry
    )
    coordinator.start_session(DEMO_SESSION_ID, case_id=bootstrap.case_id, pm_actor_id=bootstrap.pm_actor_id)
    registered_by_id = {
        identity.artifact_id: identity
        for identity in bootstrap.registered_source_identities
    }
    evidence_snapshots = (
            RegisteredMarkdownSnapshot.from_registered(
                EvidenceSourceRole.PM_SPEC,
                registered_by_id[bootstrap.pm_source_id],
                catalog.read_text(SourceName.PM_SPEC),
            ),
            RegisteredMarkdownSnapshot.from_registered(
                EvidenceSourceRole.TECHNICAL_SPEC,
                registered_by_id[bootstrap.technical_source_id],
                catalog.read_text(SourceName.TECHNICAL_SPEC),
            ),
        )
    evidence_index = EvidenceIndexer().build(
        case_id=bootstrap.case_id,
        snapshots=evidence_snapshots,
    )
    voice_provider = live_provider or GeminiLiveProvider(
        api_key=runtime_settings.gemini_api_key.get_secret_value(),
        model=runtime_settings.gemini_model,
    )
    resolved_analyzer = analyzer_provider
    if resolved_analyzer is None and live_provider is None:
        resolved_analyzer = TerraResponsesProvider(
            api_key=runtime_settings.openai_api_key.get_secret_value(),
            model=runtime_settings.terra_model,
        )
    gate = None
    analyzer_business_context = build_analyzer_business_context(catalog)
    if resolved_analyzer is not None:
        grounding = grounding_checker or RegisteredEvidenceGrounding(
            static_refs={
                bootstrap.pm_source_id: (
                    1,
                    catalog.digest(SourceName.PM_SPEC),
                    tuple(line for _, line in catalog.numbered_lines(SourceName.PM_SPEC)),
                ),
                bootstrap.technical_source_id: (
                    1,
                    catalog.digest(SourceName.TECHNICAL_SPEC),
                    tuple(line for _, line in catalog.numbered_lines(SourceName.TECHNICAL_SPEC)),
                ),
            },
            store=workshop_store, session_id=DEMO_SESSION_ID,
        )
        gate = WorkshopGate(
            workshop_store,
            workflow,
            resolved_analyzer,
            grounding,
            clock=runtime_clock,
            evidence_index=evidence_index,
            evidence_snapshots=evidence_snapshots,
            business_context=analyzer_business_context,
            dev_lead_actor_id=bootstrap.dev_lead_actor_id,
            telemetry=telemetry,
            default_effort=runtime_settings.analyzer_reasoning_effort,
        )
    finish_coordinator = None if gate is None else FinishCoordinator(
        workshop_store, workflow, gate, clock=runtime_clock
    )
    final_turn_processor = FinalTurnProcessor(coordinator, gate)
    live_transport = LiveTransport(
        voice_provider, coordinator, workshop_store,
        session_id=DEMO_SESSION_ID, idle_timeout_seconds=idle_timeout_seconds, gate=gate,
        finish_coordinator=finish_coordinator, telemetry=telemetry,
        final_turn_processor=final_turn_processor,
    )
    app = FastAPI(title="SpecOps Workshop", docs_url=None, redoc_url=None)
    app.state.settings = runtime_settings
    app.state.workflow = workflow
    app.state.bootstrap = bootstrap
    app.state.source_catalog = catalog
    app.state.evidence_index = evidence_index
    app.state.evidence_snapshots = evidence_snapshots
    app.state.workshop_store = workshop_store
    app.state.coordinator = coordinator
    app.state.session_id = DEMO_SESSION_ID
    app.state.live_provider = voice_provider
    app.state.live_transport = live_transport
    app.state.gate = gate
    app.state.final_turn_processor = final_turn_processor
    app.state.analyzer_business_context = analyzer_business_context
    app.state.finish_coordinator = finish_coordinator
    app.state.telemetry = telemetry

    @app.get("/api/bootstrap", response_model=BootstrapView)
    async def bootstrap_view() -> BootstrapView:
        return app.state.bootstrap

    @app.get("/api/session", response_model=RecoveryView)
    async def recover_session() -> RecoveryView:
        return app.state.coordinator.recover(app.state.session_id)

    @app.get("/api/analyzer/recovery")
    async def analyzer_recovery():
        if app.state.gate is None:
            return None
        return app.state.gate.latest_recovery(app.state.session_id)

    @app.post("/api/session/final-turn")
    async def final_turn(value: FinalTurnInput):
        processed = await app.state.final_turn_processor.process(
            app.state.session_id,
            turn_sequence=value.turn_sequence,
            text=value.text,
            provider_request_id=value.provider_request_id,
            correction_of_version=value.correction_of_version,
        )
        return processed.committed

    @app.post("/api/telemetry/spans", response_model=LatencySpan)
    async def browser_latency_span(value: BrowserSpanInput) -> LatencySpan:
        return app.state.telemetry.record_browser(app.state.session_id, value)

    @app.get("/api/workshop", response_model=WorkshopProjection)
    async def workshop_projection() -> WorkshopProjection:
        recovered = app.state.coordinator.recover(app.state.session_id)
        pending = app.state.workshop_store.pending_proposal(app.state.session_id)
        proposal = None if pending is None else PendingProposalView(
            record=pending,
            result=AnalyzerTurnResult.model_validate_json(pending.analyzer_result_json),
        )
        query = QueryOne(case_id=bootstrap.case_id, acting_actor_id=bootstrap.pm_actor_id)
        try:
            governance = app.state.workflow.get_spec_package_governance(query)
        except DomainError:
            governance = None
        reviews = app.state.workflow.list_review_requests(UUIDListQuery(
            case_id=bootstrap.case_id,
            acting_actor_id=bootstrap.pm_actor_id,
        )).items
        handoff = None
        if recovered.session.conversation_phase.value == "HANDOFF_READY":
            handoff = app.state.workflow.get_downstream_handoff(query)
        return WorkshopProjection(
            session=recovered.session,
            final_transcripts=recovered.final_transcripts,
            pending_proposal=proposal,
            governance=governance,
            review_requests=tuple(reviews),
            handoff=handoff,
        )

    @app.post("/api/finish", response_model=FinishResult)
    async def finish_workshop() -> FinishResult:
        if app.state.finish_coordinator is None:
            raise HTTPException(status_code=503, detail="Final audit is unavailable")
        try:
            return await app.state.finish_coordinator.finish(app.state.session_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @app.post("/api/proposals/control")
    async def proposal_control(value: ProposalControlInput):
        if app.state.gate is None:
            raise HTTPException(status_code=503, detail="Analyzer gate is unavailable")
        snapshots = app.state.workshop_store.latest_snapshots(app.state.session_id)
        if not snapshots:
            raise HTTPException(status_code=409, detail="No final PM evidence is available")
        control = AnalyzerTurnResult(
            schema_version=1,
            turn_source_ref=snapshots[-1].final_source_ref,
            finding_proposals=[],
            complete_package_proposal=None,
            control_intent=ControlIntent(value.intent),
            control_target=ControlTarget.WORKSHOP_PATCH,
            target_proposal_ref=value.proposal_ref,
            edit_instruction=value.edit_instruction,
            acknowledgement=value.acknowledgement,
            next_question=None,
        )
        try:
            result = app.state.gate.apply_control(
                app.state.session_id, control, confirmation_context=True
            )
            if value.intent == ControlIntent.EDIT:
                await app.state.gate.analyze_final_turn(
                    app.state.session_id,
                    snapshots[-1].turn_sequence,
                    edit_instruction=value.edit_instruction,
                )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return {"status": None if result is None else result.status.value}

    @app.websocket("/ws/live")
    async def live_socket(websocket: WebSocket) -> None:
        await app.state.live_transport.handle(websocket)

    frontend_dist = BACKEND_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            candidate = frontend_dist / path
            if path and candidate.is_file() and candidate.resolve().is_relative_to(frontend_dist.resolve()):
                return FileResponse(candidate)
            index = frontend_dist / "index.html"
            if index.is_file():
                return FileResponse(index)
            raise HTTPException(status_code=404)
    return app
