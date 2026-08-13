"""FastAPI publication surface for Workshop Protocol 1.0.0."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from specops_contracts import workshop_v1 as c
from specops_contracts import artifact_quality_v1 as q
from specops_workflow.workshop_protocol import FoundationProtocolError, WorkshopFoundationService

from .openai_adapter import safe_validation_diagnostics
from .artifact_quality_adapter import ArtifactQualityEvaluatorError


router = APIRouter(prefix="/api/v4", tags=["Workshop Protocol 1.0.0"])


class FoundationErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    code: c.FoundationRejectionCode


class FoundationErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    detail: FoundationErrorResponse


class ContractValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    diagnostics: tuple[c.SafeValidationDiagnostic, ...]


class ContractValidationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    detail: ContractValidationResponse


class ArtifactReviewProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    confirmation_id: UUID
    view_id: UUID
    view_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    view: dict
    confirmed: bool


class ConfirmedArtifactProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    artifact_id: UUID
    artifact_key: str
    artifact_version: int
    record_revision: int
    payload_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    payload: dict


class VoiceSelectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    selection: c.VoiceConfirmationSelectionCandidate
    actor_authentication: c.ActorAuthentication


class ArtifactSynthesisIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ArtifactSynthesisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    synthesis: c.ArtifactSynthesisAdmissionReceipt
    quality_audit: q.ArtifactQualityAuditReceipt


class ArtifactReviewIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ArtifactConfirmationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    actor_authentication: c.ActorAuthentication
    confirmation_transcript_event_id: UUID


class DecisionResponseIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
    response_transcript_event_id: UUID
    actor_authentication: c.ActorAuthentication
    selections: Annotated[
        tuple[c.VoiceConfirmationSelectionItemCandidate, ...],
        Field(min_length=1, max_length=26),
    ]




def _foundation(request: Request) -> WorkshopFoundationService:
    return request.app.state.workshop_protocol_foundation


def _execute(request: Request, command: c.FoundationCommand) -> c.FoundationReceipt:
    try:
        return _foundation(request).execute(command)
    except FoundationProtocolError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code.value}) from None


def _request_body(schema: dict) -> dict:
    return {
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": schema}},
        }
    }


def _raw_parser(contract):
    adapter = TypeAdapter(contract)

    async def parse(request: Request):
        try:
            return adapter.validate_json(await request.body(), strict=True)
        except ValidationError as exc:
            diagnostics = safe_validation_diagnostics(exc)
            raise HTTPException(
                status_code=422,
                detail={
                    "diagnostics": [item.model_dump(mode="json") for item in diagnostics]
                },
            ) from None

    schema = adapter.json_schema(
        mode="validation",
        ref_template="#/components/schemas/V4_{model}",
    )
    definitions = schema.pop("$defs", {})
    return parse, schema, {f"V4_{name}": value for name, value in definitions.items()}


_parse_foundation_command, _foundation_command_schema, _foundation_definitions = _raw_parser(
    c.FoundationCommand
)
_parse_final_transcript, _final_transcript_schema, _transcript_definitions = _raw_parser(
    c.RecordFinalTranscriptCommand
)
_parse_decision_response, _decision_response_schema, _decision_definitions = _raw_parser(
    c.ApplyDecisionBatchResponseCommand
)
_parse_voice_selection, _voice_selection_schema, _voice_selection_definitions = _raw_parser(
    VoiceSelectionInput
)
_parse_artifact_confirmation, _artifact_confirmation_schema, _artifact_confirmation_definitions = _raw_parser(
    ArtifactConfirmationIntent
)
_parse_decision_intent, _decision_intent_schema, _decision_intent_definitions = _raw_parser(
    DecisionResponseIntent
)
_RAW_CONTRACT_DEFINITIONS = {
    **_foundation_definitions,
    **_transcript_definitions,
    **_decision_definitions,
    **_voice_selection_definitions,
    **_artifact_confirmation_definitions,
    **_decision_intent_definitions,
}


@router.post(
    "/foundation/commands",
    response_model=c.FoundationReceipt,
    responses={
        409: {"model": FoundationErrorEnvelope},
        422: {"model": ContractValidationEnvelope},
    },
    openapi_extra=_request_body(_foundation_command_schema),
)
async def execute_foundation_command(
    request: Request, command=Depends(_parse_foundation_command)
) -> c.FoundationReceipt:
    return _execute(request, command)


@router.post(
    "/voice/final-transcripts",
    response_model=c.TranscriptRecordedReceipt,
    responses={
        409: {"model": FoundationErrorEnvelope},
        422: {"model": ContractValidationEnvelope},
    },
    openapi_extra=_request_body(_final_transcript_schema),
)
async def record_voice_final_transcript(
    request: Request, command=Depends(_parse_final_transcript)
) -> c.TranscriptRecordedReceipt:
    # Deliberately the same handler as the generic Foundation command seam.
    return _execute(request, command)  # type: ignore[return-value]


@router.post(
    "/voice/decision-responses",
    response_model=c.DecisionBatchResponseReceipt,
    responses={
        409: {"model": FoundationErrorEnvelope},
        422: {"model": ContractValidationEnvelope},
    },
    openapi_extra=_request_body(_decision_response_schema),
)
async def apply_voice_decision_response(
    request: Request, command=Depends(_parse_decision_response)
) -> c.DecisionBatchResponseReceipt:
    return _execute(request, command)  # type: ignore[return-value]


@router.post(
    "/voice/decision-selections",
    response_model=c.DecisionBatchResponseReceipt,
    responses={422: {"model": ContractValidationEnvelope}},
    openapi_extra=_request_body(_voice_selection_schema),
)
async def apply_voice_decision_selection(
    request: Request, value=Depends(_parse_voice_selection)
) -> c.DecisionBatchResponseReceipt:
    orchestrator = request.app.state.workshop_protocol_orchestrator
    try:
        return orchestrator.apply_voice_selection(
            value.selection, value.actor_authentication
        )
    except FoundationProtocolError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code.value}) from None


@router.post(
    "/decisions/current/respond",
    response_model=c.DecisionBatchResponseReceipt,
    responses={422: {"model": ContractValidationEnvelope}},
    openapi_extra=_request_body(_decision_intent_schema),
)
async def apply_browser_decision_selection(
    request: Request, value=Depends(_parse_decision_intent)
) -> c.DecisionBatchResponseReceipt:
    try:
        return request.app.state.workshop_protocol_orchestrator.apply_review_selection(
            operation_key=value.operation_key,
            response_transcript_event_id=value.response_transcript_event_id,
            selections=value.selections,
            authentication=value.actor_authentication,
        )
    except FoundationProtocolError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code.value}) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from None


@router.post(
    "/artifacts/{artifact_type}/synthesize",
    response_model=ArtifactSynthesisResult,
)
async def synthesize_artifact(
    request: Request,
    artifact_type: Annotated[str, Field(pattern=r"^(SPEC_PACKAGE|TECHNICAL_CONTRACT)$")],
    value: ArtifactSynthesisIntent,
):
    try:
        admission = await request.app.state.workshop_protocol_orchestrator.synthesize_artifact(
            artifact_type, operation_key=value.operation_key
        )
        if not isinstance(admission.receipt, c.ArtifactSynthesisAdmissionReceipt):
            raise RuntimeError("synthesis did not return an artifact receipt")
        if admission.quality_audit is None:
            raise RuntimeError("production synthesis omitted its quality audit")
        return ArtifactSynthesisResult(
            synthesis=admission.receipt,
            quality_audit=admission.quality_audit,
        )
    except ArtifactQualityEvaluatorError as exc:
        raise HTTPException(
            status_code=503,
            detail=exc.receipt.model_dump(mode="json"),
        ) from None
    except FoundationProtocolError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code.value}) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from None


@router.post(
    "/artifacts/{artifact_type}/review",
    response_model=c.ArtifactReviewReceipt,
)
async def materialize_artifact_review(
    request: Request,
    artifact_type: Annotated[str, Field(pattern=r"^(SPEC_PACKAGE|TECHNICAL_CONTRACT)$")],
    value: ArtifactReviewIntent,
):
    try:
        return request.app.state.workshop_protocol_orchestrator.materialize_latest_artifact_review(
            artifact_type, operation_key=value.operation_key
        )
    except FoundationProtocolError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code.value}) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from None


@router.post(
    "/artifacts/current/confirm",
    response_model=c.ArtifactConfirmationReceipt,
    responses={422: {"model": ContractValidationEnvelope}},
    openapi_extra=_request_body(_artifact_confirmation_schema),
)
async def confirm_current_artifact(
    request: Request, value=Depends(_parse_artifact_confirmation)
):
    try:
        return request.app.state.workshop_protocol_orchestrator.confirm_current_artifact(
            actor_authentication=value.actor_authentication,
            confirmation_transcript_event_id=value.confirmation_transcript_event_id,
        )
    except FoundationProtocolError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code.value}) from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from None




@router.get("/cases/{case_id}/decision-review", response_model=c.DecisionBatchReviewView | None)
async def current_decision_review(request: Request, case_id: UUID):
    return _foundation(request).current_decision_view(case_id)


@router.get("/cases/{case_id}/artifact-review", response_model=ArtifactReviewProjection | None)
async def current_artifact_review(request: Request, case_id: UUID):
    value = _foundation(request).current_artifact_review(case_id)
    return None if value is None else ArtifactReviewProjection.model_validate(value)


@router.get(
    "/cases/{case_id}/artifacts/{artifact_type}",
    response_model=ConfirmedArtifactProjection | None,
)
async def confirmed_artifact(
    request: Request,
    case_id: UUID,
    artifact_type: Annotated[str, Field(pattern=r"^(SPEC_PACKAGE|TECHNICAL_CONTRACT)$")],
):
    value = _foundation(request).confirmed_artifact(case_id, artifact_type)
    return None if value is None else ConfirmedArtifactProjection.model_validate(value)


def install_workshop_protocol_api(app: FastAPI, foundation: WorkshopFoundationService) -> None:
    app.state.workshop_protocol_foundation = foundation
    # Append concrete routes so the established runtime's route-inspection
    # safety checks continue to see only path-bearing route objects.
    app.router.routes.extend(router.routes)
    _install_raw_contract_definitions(app)


def _install_raw_contract_definitions(app: FastAPI) -> None:
    base_openapi = app.openapi

    def openapi_with_protocol_contracts():
        schema = base_openapi()
        schema.setdefault("components", {}).setdefault("schemas", {}).update(
            _RAW_CONTRACT_DEFINITIONS
        )
        return schema

    app.openapi = openapi_with_protocol_contracts


def create_contract_app() -> FastAPI:
    """Schema-only application used by deterministic code generation."""

    app = FastAPI(title="SpecOps Workshop Protocol", version="1.0.0")
    app.router.routes.extend(router.routes)
    _install_raw_contract_definitions(app)
    return app
