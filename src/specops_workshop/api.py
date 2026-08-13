"""Production FastAPI application for the deterministic V4 Workshop seam."""

from __future__ import annotations

import hashlib
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from specops_contracts import workshop_v1 as c
import specops_contracts
from specops_workflow import SystemClock
from specops_workflow.workshop_protocol import FoundationProtocolError, WorkshopFoundationService

from .bootstrap import BootstrapView, bootstrap_foundation, stable_id
from .boundary import assert_downstream_boundary
from .config import Settings
from .contracts import FinalTurnInput
from .delegation import load_delegation_fixture
from .ports import LiveVoiceProvider
from .providers import GeminiLiveProvider
from .sources import SourceCatalog, SourceName
from .v4.api import install_workshop_protocol_api
from .v4.live_transport import V4LiveTransport
from .v4.client_presence import WorkshopClientPresence
from .v4.openai_adapter import ProviderAdapterError, ProviderSourceUpload, StoredConversationOpenAIAdapter
from .v4.artifact_quality_adapter import (
    ArtifactQualityEvaluatorError,
    FreshConversationTerraQualityEvaluator,
)
from .v4.orchestrator import FinalTranscriptInput, V4ProductionOrchestrator
from .v4.scheduler import DurableAnalyzerWorker


BACKEND_ROOT = Path(__file__).resolve().parents[2]
SPEC_ENG_ROOT = next(
    (
        candidate
        for candidate in (BACKEND_ROOT.parent / "Spec_Eng", *BACKEND_ROOT.parents)
        if (candidate / "docs").is_dir() and (candidate / "app").is_dir()
    ),
    BACKEND_ROOT.parent / "Spec_Eng",
)
V4_ROOT = SPEC_ENG_ROOT / "spec-workshop-contracts-and-interaction-model-v4"
QUALITY_CONTRACT_PATH = Path(specops_contracts.__file__).resolve().parent / "semantic-quality-contract.yaml"
DEMO_SESSION_ID = stable_id("csv-export-workshop:session")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _analyzer_contract() -> c.AnalyzerContractBinding:
    instruction = (
        b"SpecOps Workshop Analyzer 1: propose semantics only through six narrow operations; "
        b"Foundation owns identity, authority, confirmation, readiness, audit, and handoff."
    )
    return c.AnalyzerContractBinding(
        protocol_version=c.PROTOCOL_VERSION,
        instruction_set_id="specops-workshop-analyzer",
        instruction_set_version=1,
        instruction_set_hash="sha256:" + hashlib.sha256(instruction).hexdigest(),
        semantic_quality_contract_id="SEMANTIC-QUALITY-CONTRACT",
        semantic_quality_contract_version="2.1.0",
        semantic_quality_contract_hash=_sha256(QUALITY_CONTRACT_PATH),
        provider_schema_version="1.0.0",
        model="gpt-5.6-terra",
        reasoning_effort=c.ReasoningEffort.MEDIUM,
    )


def create_app(
    *,
    settings: Settings | None = None,
    clock=None,
    source_catalog: SourceCatalog | None = None,
    live_provider: LiveVoiceProvider | None = None,
    analyzer_adapter: StoredConversationOpenAIAdapter | Any | None = None,
    quality_evaluator: Any | None = None,
) -> FastAPI:
    runtime_clock = clock or SystemClock()
    assert_downstream_boundary(BACKEND_ROOT)
    runtime_settings = settings or Settings.load(
        os.environ,
        Path(os.environ.get("SPECOPS_ENV_FILE", SPEC_ENG_ROOT / ".env")),
    )
    catalog = source_catalog or SourceCatalog(SPEC_ENG_ROOT)
    fixture = load_delegation_fixture(catalog, now=runtime_clock.now())
    _, bootstrap = bootstrap_foundation(
        runtime_settings, catalog, fixture, clock=runtime_clock
    )
    sources = tuple(
        ProviderSourceUpload(
            source=c.SourceIdentity(
                source_id=source_id,
                role=role,
                version=1,
                payload_hash="sha256:" + catalog.digest(name),
                canonical_locator=str(catalog.document(name).path),
                filename=catalog.document(name).path.name,
                media_type="text/markdown",
            ),
            content=catalog.read_bytes(name),
        )
        for source_id, role, name in (
            (bootstrap.pm_source_id, c.SourceRole.PM_SPEC, SourceName.PM_SPEC),
            (
                bootstrap.v4_technical_contract_source_id,
                c.SourceRole.TECHNICAL_CONTRACT,
                SourceName.TECHNICAL_CONTRACT,
            ),
        )
    )
    source_set_hash = StoredConversationOpenAIAdapter.source_set_hash(
        tuple(item.source for item in sources)
    )
    foundation = WorkshopFoundationService(
        runtime_settings.specops_database_url, now=runtime_clock.now
    )
    foundation.register_case(
        case_id=bootstrap.case_id,
        session_id=DEMO_SESSION_ID,
        source_set_hash=source_set_hash,
    )
    adapter = analyzer_adapter or StoredConversationOpenAIAdapter(
        api_key=runtime_settings.openai_api_key.get_secret_value(), now=runtime_clock.now
    )
    evaluator = quality_evaluator or FreshConversationTerraQualityEvaluator(
        api_key=runtime_settings.openai_api_key.get_secret_value(), now=runtime_clock.now
    )
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=bootstrap.case_id,
        session_id=DEMO_SESSION_ID,
        sources=sources,
        analyzer_contract=_analyzer_contract(),
        quality_evaluator=evaluator,
        now=runtime_clock.now,
    )
    voice = live_provider or GeminiLiveProvider(
        api_key=runtime_settings.gemini_api_key.get_secret_value(),
        model=runtime_settings.gemini_model,
    )
    live_transport = V4LiveTransport(voice, orchestrator)
    client_presence = WorkshopClientPresence(orchestrator)

    worker = DurableAnalyzerWorker(
        orchestrator, has_active_clients=client_presence.has_active_clients
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker_task = asyncio.create_task(worker.run_forever())
        try:
            yield
        finally:
            worker.stop()
            await asyncio.gather(worker_task, return_exceptions=True)

    app = FastAPI(
        title="SpecOps Workshop", docs_url=None, redoc_url=None, lifespan=lifespan
    )
    app.state.settings = runtime_settings
    app.state.bootstrap = bootstrap
    app.state.source_catalog = catalog
    app.state.session_id = DEMO_SESSION_ID
    app.state.live_provider = voice
    app.state.live_transport = live_transport
    app.state.client_presence = client_presence
    app.state.v4_source_uploads = sources
    app.state.v4_source_set_hash = source_set_hash
    app.state.workshop_protocol_orchestrator = orchestrator
    app.state.openai_adapter = adapter
    app.state.artifact_quality_evaluator = evaluator
    app.state.analyzer_worker = worker
    install_workshop_protocol_api(app, foundation)

    @app.get("/api/bootstrap", response_model=BootstrapView)
    async def bootstrap_view() -> BootstrapView:
        return bootstrap

    @app.get("/api/workshop")
    async def workshop_projection():
        case = foundation.get_case(bootstrap.case_id)
        transcripts = foundation.final_transcripts(bootstrap.case_id)
        preparation = foundation.preparation_projection(bootstrap.case_id)
        runway = foundation.runway_projection(bootstrap.case_id)
        voice_card = foundation.voice_session_card(bootstrap.case_id)
        return {
            "protocol_version": c.PROTOCOL_VERSION,
            "case_id": str(case.case_id),
            "session_id": str(case.session_id),
            "case_revision": foundation.case_revision(case.case_id),
            "readiness": case.readiness.value,
            "review_obligation": case.review_obligation.value,
            "session": {
                "workshop_state": "ACTIVE",
                "conversation_phase": "WORKSHOP",
                "call_state": "READY" if preparation["phase"] == "READY" else "PREPARING",
                "revision_locked": False,
                "revision_lock_reason": None,
            },
            "preparation": preparation,
            "runway": runway,
            "voice_session_card": voice_card.model_dump(mode="json"),
            "analyzer_jobs": [
                {
                    "job_id": item["job_id"],
                    "operation": item["operation"],
                    "state": item["state"],
                    "attempt_count": item["attempt_count"],
                }
                for item in foundation.analyzer_jobs(bootstrap.case_id)
            ],
            "final_transcripts": [
                {
                    "event_id": str(item.event_id),
                    "turn_sequence": item.sequence_number,
                    "version": item.transcript_version,
                    "normalized_text": item.text,
                    "correction_of_version": None,
                    "speaker_actor_id": (
                        None if item.speaker_actor_id is None else str(item.speaker_actor_id)
                    ),
                }
                for item in transcripts
            ],
            "pending_proposal": None,
            "governance": None,
            "review_requests": [],
            "handoff": None,
            "analyzer_context_status": (
                "ACTIVE"
                if foundation.active_analyzer_context(case.case_id) is not None
                else "REBUILD_REQUIRED"
            ),
            "last_final_transcript_sequence": (
                None if not transcripts else transcripts[-1].sequence_number
            ),
            "decision_review": foundation.current_decision_view(case.case_id),
            "artifact_review": foundation.current_artifact_review(case.case_id),
        }

    @app.post("/api/session/final-turn")
    async def final_turn(value: FinalTurnInput):
        try:
            return await orchestrator.record_final_transcript(
                FinalTranscriptInput(
                    turn_sequence=value.turn_sequence,
                    text=value.text,
                    provider_request_id=value.provider_request_id,
                    speaker_actor_id=bootstrap.pm_actor_id,
                    actor="PM",
                )
            )
        except ProviderAdapterError as exc:
            raise HTTPException(
                status_code=503,
                detail=exc.receipt.model_dump(mode="json"),
            ) from None
        except (FoundationProtocolError, ValueError) as exc:
            code = exc.code.value if isinstance(exc, FoundationProtocolError) else str(exc)
            raise HTTPException(status_code=409, detail={"code": code}) from None

    @app.websocket("/ws/live")
    async def live_socket(websocket: WebSocket) -> None:
        await live_transport.handle(websocket)

    @app.websocket("/ws/presence")
    async def presence_socket(websocket: WebSocket) -> None:
        await client_presence.handle(websocket)

    frontend_dist = BACKEND_ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        assets = frontend_dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def frontend(path: str):
            candidate = frontend_dist / path
            if (
                path
                and candidate.is_file()
                and candidate.resolve().is_relative_to(frontend_dist.resolve())
            ):
                return FileResponse(candidate)
            index = frontend_dist / "index.html"
            if index.is_file():
                return FileResponse(index)
            raise HTTPException(status_code=404)
    return app
