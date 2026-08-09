from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from specops_workflow import SystemClock

from .bootstrap import BootstrapView, bootstrap_foundation, stable_id
from .config import Settings
from .contracts import FinalTurnInput, RecoveryView
from .delegation import load_delegation_fixture
from .gate import RegisteredEvidenceGrounding, WorkshopGate
from .live_transport import LiveTransport
from .orchestration import WorkshopCoordinator
from .ports import LiveVoiceProvider
from .providers import GeminiLiveProvider
from .providers.openai_responses import TerraResponsesProvider
from .sessions import WorkshopStore
from .sources import SourceCatalog, SourceName


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
    coordinator = WorkshopCoordinator(workshop_store, workflow, clock=runtime_clock)
    coordinator.start_session(DEMO_SESSION_ID, case_id=bootstrap.case_id, pm_actor_id=bootstrap.pm_actor_id)
    voice_provider = live_provider or GeminiLiveProvider(
        api_key=runtime_settings.gemini_api_key,
        model=runtime_settings.gemini_model,
    )
    resolved_analyzer = analyzer_provider
    if resolved_analyzer is None and live_provider is None:
        resolved_analyzer = TerraResponsesProvider(api_key=runtime_settings.openai_api_key, model=runtime_settings.terra_model)
    gate = None
    if resolved_analyzer is not None:
        grounding = grounding_checker or RegisteredEvidenceGrounding(
            static_refs={
                bootstrap.pm_source_id: (
                    1,
                    catalog.digest(SourceName.PM_SPEC),
                    len(catalog.numbered_lines(SourceName.PM_SPEC)),
                ),
                bootstrap.technical_source_id: (
                    1,
                    catalog.digest(SourceName.TECHNICAL_SPEC),
                    len(catalog.numbered_lines(SourceName.TECHNICAL_SPEC)),
                ),
            },
            store=workshop_store, session_id=DEMO_SESSION_ID,
        )
        gate = WorkshopGate(workshop_store, workflow, resolved_analyzer, grounding, clock=runtime_clock)
    live_transport = LiveTransport(
        voice_provider, coordinator, workshop_store,
        session_id=DEMO_SESSION_ID, idle_timeout_seconds=idle_timeout_seconds, gate=gate,
    )
    app = FastAPI(title="SpecOps Workshop", docs_url=None, redoc_url=None)
    app.state.settings = runtime_settings
    app.state.workflow = workflow
    app.state.bootstrap = bootstrap
    app.state.source_catalog = catalog
    app.state.workshop_store = workshop_store
    app.state.coordinator = coordinator
    app.state.session_id = DEMO_SESSION_ID
    app.state.live_provider = voice_provider
    app.state.live_transport = live_transport
    app.state.gate = gate

    @app.get("/api/bootstrap", response_model=BootstrapView)
    async def bootstrap_view() -> BootstrapView:
        return app.state.bootstrap

    @app.get("/api/session", response_model=RecoveryView)
    async def recover_session() -> RecoveryView:
        return app.state.coordinator.recover(app.state.session_id)

    @app.post("/api/session/final-turn")
    async def final_turn(value: FinalTurnInput):
        return app.state.coordinator.commit_final_turn(
            app.state.session_id,
            turn_sequence=value.turn_sequence,
            text=value.text,
            provider_request_id=value.provider_request_id,
            correction_of_version=value.correction_of_version,
        )

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
