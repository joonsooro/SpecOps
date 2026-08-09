from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from openai import AsyncOpenAI

from ..analyzer import (
    AnalyzerProviderAvailabilityError,
    AnalyzerProviderSchemaError,
    SemanticAnalyzerRequest,
    SemanticTurnDraft,
    SpecAnalyzerProvider,
)
from ..config import TERRA_MODEL
from ..privacy_egress import TerraPrivacyEgressGateway, assert_identity_free_schema


TERRA_REQUEST_TIMEOUT_SECONDS = 30.0
TERRA_SEMANTIC_MAX_OUTPUT_TOKENS = 8192
TerraRequestStage = Literal["ANALYSIS"]


def _safe_request_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512 or not value.isascii():
        return None
    if not all(character.isalnum() or character in "._-" for character in value):
        return None
    return value


@dataclass(frozen=True)
class TerraRequestDiagnostic:
    stage: TerraRequestStage
    outcome: Literal["PASS", "FAIL"]
    client_request_id: str
    provider_request_id: str | None
    status_code: int | None
    error_type: str | None

    def as_receipt(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "outcome": self.outcome,
            "client_request_id": self.client_request_id,
            "provider_request_id": self.provider_request_id,
            "status_code": self.status_code,
            "error_type": self.error_type,
        }


class TerraProviderRequestError(AnalyzerProviderAvailabilityError):
    def __init__(
        self,
        *,
        stage: TerraRequestStage,
        client_request_id: str,
        provider_request_id: str | None,
        status_code: int | None,
        request_diagnostics: tuple[TerraRequestDiagnostic, ...],
    ) -> None:
        super().__init__(f"TERRA_{stage}_FAILURE")
        self.stage = stage
        self.client_request_id = client_request_id
        self.provider_request_id = provider_request_id
        self.status_code = status_code
        self.request_diagnostics = request_diagnostics


def _openai_strict_schema(value):
    if isinstance(value, dict):
        result = {
            ("anyOf" if key == "oneOf" else key): _openai_strict_schema(item)
            for key, item in value.items()
            if key != "discriminator"
        }
        properties = result.get("properties")
        if isinstance(properties, dict):
            result["required"] = list(properties)
            result["additionalProperties"] = False
        return result
    if isinstance(value, list):
        return [_openai_strict_schema(item) for item in value]
    return value


class TerraResponsesProvider(SpecAnalyzerProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str = TERRA_MODEL,
        client=None,
        egress_gateway: TerraPrivacyEgressGateway | None = None,
    ) -> None:
        if model != TERRA_MODEL:
            raise ValueError("Terra model must match the pinned Workshop model")
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=TERRA_REQUEST_TIMEOUT_SECONDS,
        )
        self._model = model
        self._egress_gateway = egress_gateway or TerraPrivacyEgressGateway()
        self._request_diagnostics: deque[TerraRequestDiagnostic] = deque(maxlen=8)

    @property
    def request_diagnostics(self) -> tuple[TerraRequestDiagnostic, ...]:
        return tuple(self._request_diagnostics)

    async def analyze(self, request: SemanticAnalyzerRequest) -> SemanticTurnDraft:
        if request.effort != "medium":
            raise ValueError("Workshop Terra analysis requires explicit medium effort")
        content = self._egress_gateway.content_blocks(request)
        self._egress_gateway.manifest(content)
        schema = _openai_strict_schema(
            SemanticTurnDraft.model_json_schema(mode="validation")
        )
        assert_identity_free_schema(schema)
        response = await self._create_response(
            request=request,
            content=content,
            schema=schema,
        )
        try:
            return SemanticTurnDraft.model_validate_json(response.output_text)
        except Exception as exc:
            self._record_output_failure("SEMANTIC_VALIDATION", exc)
            raise AnalyzerProviderSchemaError(
                "provider response failed strict semantic validation"
            ) from exc

    def _record_output_failure(self, stage_name: str, exc: Exception) -> None:
        previous = self._request_diagnostics[-1] if self._request_diagnostics else None
        self._request_diagnostics.append(
            TerraRequestDiagnostic(
                stage="ANALYSIS",
                outcome="FAIL",
                client_request_id=(
                    previous.client_request_id if previous is not None else "unavailable"
                ),
                provider_request_id=(
                    previous.provider_request_id if previous is not None else None
                ),
                status_code=(previous.status_code if previous is not None else None),
                error_type=f"{stage_name}_{type(exc).__name__}",
            )
        )

    async def _create_response(self, *, request, content, schema):
        client_request_id = f"specops-workshop-analysis-{uuid4()}"
        arguments = {
            "model": self._model,
            "reasoning": {"effort": request.effort},
            "store": False,
            "extra_headers": {"X-Client-Request-Id": client_request_id},
            "input": [{"role": "user", "content": list(content)}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "semantic_turn_draft_v1",
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": TERRA_SEMANTIC_MAX_OUTPUT_TOKENS,
        }
        try:
            response = await asyncio.wait_for(
                self._client.responses.create(**arguments),
                timeout=TERRA_REQUEST_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            provider_request_id = _safe_request_identifier(
                getattr(exc, "request_id", None)
            )
            status_code = getattr(exc, "status_code", None)
            if not isinstance(status_code, int):
                status_code = None
            self._request_diagnostics.append(
                TerraRequestDiagnostic(
                    stage="ANALYSIS",
                    outcome="FAIL",
                    client_request_id=client_request_id,
                    provider_request_id=provider_request_id,
                    status_code=status_code,
                    error_type=type(exc).__name__,
                )
            )
            raise TerraProviderRequestError(
                stage="ANALYSIS",
                client_request_id=client_request_id,
                provider_request_id=provider_request_id,
                status_code=status_code,
                request_diagnostics=self.request_diagnostics,
            ) from exc
        self._request_diagnostics.append(
            TerraRequestDiagnostic(
                stage="ANALYSIS",
                outcome="PASS",
                client_request_id=client_request_id,
                provider_request_id=_safe_request_identifier(
                    getattr(response, "_request_id", None)
                ),
                status_code=200,
                error_type=None,
            )
        )
        return response
