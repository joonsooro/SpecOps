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
from .delegation import load_delegation_fixture
from .ports import LiveVoiceProvider
from .sources import SourceCatalog, SourceName
from .v4.api import install_workshop_protocol_api
from .v4.client_presence import WorkshopClientPresence
from .v4.openai_adapter import ProviderAdapterError, ProviderSourceUpload, StoredConversationOpenAIAdapter
from .v4.artifact_quality_adapter import (
    ArtifactQualityEvaluatorError,
    FreshConversationTerraQualityEvaluator,
)
from .v4.orchestrator import V4ProductionOrchestrator
from .v4.scheduler import DurableAnalyzerWorker
from .chat_application import (
    ParticipantTurnError,
    ParticipantTurnIngress,
    VisualProposalActionService,
    WorkshopConversationProjector,
)
from .chat_contracts import (
    ExactTextPlaybackIntent,
    ExactTextPlaybackReceipt,
    FinishWorkshopIntent,
    SubmitTypedResponseIntent,
    VisualProposalActionIntent,
    WorkshopConversationContext,
)
from .chatbot_provider import ChatbotProvider, OpenAIResponsesChatbotProvider


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
        semantic_quality_contract_version="2.2.0",
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
    chatbot_provider: ChatbotProvider | Any | None = None,
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
    chatbot = chatbot_provider
    if chatbot is None and analyzer_adapter is None:
        chatbot = OpenAIResponsesChatbotProvider(
            api_key=runtime_settings.openai_api_key.get_secret_value()
        )
    orchestrator = V4ProductionOrchestrator(
        foundation=foundation,
        adapter=adapter,
        case_id=bootstrap.case_id,
        session_id=DEMO_SESSION_ID,
        sources=sources,
        analyzer_contract=_analyzer_contract(),
        quality_evaluator=evaluator,
        chatbot_provider=chatbot,
        now=runtime_clock.now,
    )
    client_presence = WorkshopClientPresence(orchestrator)
    projector = WorkshopConversationProjector(
        foundation,
        case_id=bootstrap.case_id,
        session_id=DEMO_SESSION_ID,
    )
    ingress = ParticipantTurnIngress(
        foundation,
        case_id=bootstrap.case_id,
        session_id=DEMO_SESSION_ID,
        actor_id=bootstrap.pm_actor_id,
    )
    proposal_actions = VisualProposalActionService(projector, orchestrator)

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
    app.state.client_presence = client_presence
    app.state.v4_source_uploads = sources
    app.state.v4_source_set_hash = source_set_hash
    app.state.workshop_protocol_orchestrator = orchestrator
    app.state.openai_adapter = adapter
    app.state.artifact_quality_evaluator = evaluator
    app.state.analyzer_worker = worker
    app.state.participant_turn_ingress = ingress
    app.state.workshop_conversation_projector = projector
    app.state.visual_proposal_actions = proposal_actions
    install_workshop_protocol_api(app, foundation, participant_runtime=True)

    @app.get("/api/bootstrap", response_model=BootstrapView)
    async def bootstrap_view() -> BootstrapView:
        return bootstrap

    @app.get("/api/workshop", response_model=WorkshopConversationContext)
    async def workshop_projection() -> WorkshopConversationContext:
        return projector.project()

    @app.get("/api/workshop/preparation")
    async def workshop_preparation():
        value = foundation.preparation_projection(bootstrap.case_id)
        phase = {
            "VALIDATING_DOCUMENTS": "UPLOADING_SOURCES",
            "PREPARING_ANALYZER": "BOOTSTRAPPING",
            "ANALYZER_REVIEWING_DOCUMENTS": "BOOTSTRAPPING",
            "FORMULATING_WORKSHOP_PLAN": "ADMITTING_GUIDANCE",
            "VALIDATING_INITIAL_RUNWAY": "ADMITTING_GUIDANCE",
            "READY": "READY",
            "FAILED": "FAILED",
        }[value["phase"]]
        message = {
            "UPLOADING_SOURCES": "Preparing Workshop sources…",
            "BOOTSTRAPPING": "Analyzing the PM Spec and Technical Contract…",
            "ADMITTING_GUIDANCE": "Preparing the first questions…",
            "READY": None,
            "FAILED": "Workshop preparation needs attention. Retry preparation.",
        }[phase]
        return {
            "phase": phase,
            "message": message,
            "delayed_message": (
                "Preparation is taking longer than 30 seconds. It is still running; "
                "you may leave and return."
                if value["delayed"] and phase not in {"READY", "FAILED"}
                else None
            ),
        }

    @app.post("/api/workshop/responses")
    async def submit_response(value: SubmitTypedResponseIntent):
        try:
            return ingress.submit_typed(value)
        except ParticipantTurnError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from None

    @app.post("/api/workshop/proposals/{proposal_ref}/confirm")
    async def confirm_proposal(proposal_ref: str, value: VisualProposalActionIntent):
        if proposal_ref != value.binding.proposal_ref:
            raise HTTPException(status_code=422, detail={"code": "MALFORMED_ACTION"})
        try:
            return proposal_actions.confirm(value)
        except (ParticipantTurnError, FoundationProtocolError, ValueError) as exc:
            code = exc.code if hasattr(exc, "code") else str(exc)
            if hasattr(code, "value"):
                code = code.value
            raise HTTPException(status_code=409, detail={"code": code}) from None

    @app.post("/api/workshop/proposals/{proposal_ref}/edit")
    async def edit_proposal(proposal_ref: str, value: VisualProposalActionIntent):
        if proposal_ref != value.binding.proposal_ref:
            raise HTTPException(status_code=422, detail={"code": "MALFORMED_ACTION"})
        try:
            return proposal_actions.edit(value)
        except ParticipantTurnError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from None

    @app.post("/api/workshop/proposals/{proposal_ref}/reject")
    async def reject_proposal(proposal_ref: str, value: VisualProposalActionIntent):
        if proposal_ref != value.binding.proposal_ref:
            raise HTTPException(status_code=422, detail={"code": "MALFORMED_ACTION"})
        try:
            return proposal_actions.reject(value)
        except ParticipantTurnError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from None

    @app.post("/api/workshop/finish")
    async def finish_workshop(value: FinishWorkshopIntent):
        context = projector.project()
        existing_completion = foundation.workshop_completion_receipt(bootstrap.case_id)
        if existing_completion is None:
            if value.expected_case_revision != context.case_revision:
                raise HTTPException(status_code=409, detail={"code": "STALE_STATE"})
            if not context.committed_turns:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "NO_COMMITTED_TURNS"},
                )
            if any(
                item.status.value in {"PENDING", "EDIT_REQUESTED"}
                for item in context.proposal_statuses
            ):
                raise HTTPException(status_code=409, detail={"code": "PENDING_PROPOSALS"})
        try:
            return await orchestrator.complete_workshop(
                operation_key=f"finish-{value.client_action_id}",
                source=c.WorkshopCompletionSource.BUTTON,
            )
        except (FoundationProtocolError, ValueError) as exc:
            code = exc.code.value if isinstance(exc, FoundationProtocolError) else str(exc)
            raise HTTPException(status_code=409, detail={"code": code}) from None

    @app.post(
        "/api/workshop/playback",
        response_model=ExactTextPlaybackReceipt,
    )
    async def prepare_playback(
        value: ExactTextPlaybackIntent,
    ) -> ExactTextPlaybackReceipt:
        context = projector.project()
        question = next(
            (
                item
                for item in context.question_runway.questions
                if item.question_id == value.question_id
                and item.question_version == value.question_version
            ),
            None,
        )
        if question is None or question.exact_text != value.exact_text:
            raise HTTPException(status_code=409, detail={"code": "STALE_QUESTION"})
        return ExactTextPlaybackReceipt(exact_text=question.exact_text)

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
