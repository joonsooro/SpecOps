from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from openai import AsyncOpenAI

from ..analyzer import AnalyzerRequest, AnalyzerTurnResult, SpecAnalyzerProvider
from ..config import TERRA_MODEL
from ..privacy_egress import TerraPrivacyEgressGateway
from .terra_decomposition import (
    TerraCacheWarmResult,
    TerraDecisionPlan,
    TerraFinishAuditPlan,
    TerraPackageOutline,
    assemble_analyzer_result,
    requires_package_outline,
)


TERRA_REQUEST_TIMEOUT_SECONDS = 30.0
TERRA_PROMPT_CACHE_KEY = "specops-workshop-terra-v0-source"
TERRA_DECISION_PLAN_MAX_OUTPUT_TOKENS = 4096
TERRA_PACKAGE_OUTLINE_MAX_OUTPUT_TOKENS = 4096
TerraRequestStage = Literal["CACHE_WARM", "ANALYSIS"]

DECISION_PLAN_INSTRUCTION = (
    "Analyze this provider-final PM Workshop turn as a compact evidence-and-decision plan. "
    "Do not generate package content in this stage. Return exactly one next unresolved TECHNICAL "
    "or CROSS_DOMAIN finding from the registered Dev Lead technical source and cite that source's "
    "exact SourceRef. Questions must be one focused interrogative of at most 25 words. Return only "
    "domain, evidence refs, and the clarification "
    "question for each finding; deterministic local policy assigns category, severity, owner, "
    "disposition, and package-change state. Each question must reuse at least one distinctive "
    "non-stopword from every cited source range. Never invent evidence, identifiers, or Jira/GitHub actions."
)

FINISH_AUDIT_INSTRUCTION = (
    "Audit the committed Workshop package against the provider-final PM evidence. Do not generate "
    "package content. Return zero findings when no new unresolved technical decision exists; otherwise "
    "return exactly one TECHNICAL or CROSS_DOMAIN finding from the registered Dev Lead technical "
    "source with its exact SourceRef and one focused question of at most 25 words. Deterministic local "
    "policy owns governance fields. Never invent evidence, identifiers, or Jira/GitHub actions."
)

PACKAGE_OUTLINE_INSTRUCTION = (
    "Using the supplied compact decision plan, generate one fixed-size grounded outline for the "
    "Workshop v0 delivery slice: exactly one item title, one business requirement, one technical "
    "decision, and one cross-domain acceptance check. Every statement must cite exact registered SourceRefs and reuse at "
    "least one distinctive non-stopword from every cited source range. Do not allocate authoritative "
    "UUIDs, proposal keys, domains, delivery flags, readiness, approvals, review obligations, or "
    "Jira/GitHub objects; deterministic local code owns those fields. Keep the four text values concise."
)


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


class TerraProviderRequestError(RuntimeError):
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
        self._warmed_prefixes: set[str] = set()
        self._cache_lock = asyncio.Lock()
        self._plan_cache = {}
        self._request_diagnostics: deque[TerraRequestDiagnostic] = deque(maxlen=8)

    @property
    def request_diagnostics(self) -> tuple[TerraRequestDiagnostic, ...]:
        return tuple(self._request_diagnostics)

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerTurnResult:
        envelope = self._egress_gateway.project(request)
        base_content = self._egress_gateway.content_blocks(
            envelope,
            cache_static_prefix=True,
        )
        is_audit = request.purpose == "FINISH_AUDIT"
        plan_instruction = FINISH_AUDIT_INSTRUCTION if is_audit else DECISION_PLAN_INSTRUCTION
        plan_type = TerraFinishAuditPlan if is_audit else TerraDecisionPlan
        plan_cache_namespace = "finish-audit" if is_audit else "decision-plan"
        decision_content = self._with_instruction(
            base_content,
            plan_instruction
            + f"\nPurpose: {request.purpose}\nPhase: {request.phase.value}",
        )
        self._egress_gateway.manifest(decision_content)
        plan = self._plan_cache.get(request.request_id)
        if plan is None:
            plan_schema = _openai_strict_schema(
                plan_type.model_json_schema(mode="validation")
            )
            await self._ensure_source_cache(
                plan_cache_namespace,
                decision_content,
                request,
            )
            response = await self._create_response(
                request=request,
                content=decision_content,
                schema=plan_schema,
                schema_name=(
                    "terra_finish_audit_v1" if is_audit else "terra_decision_plan_v1"
                ),
                stage="ANALYSIS",
                max_output_tokens=TERRA_DECISION_PLAN_MAX_OUTPUT_TOKENS,
                cache_namespace=plan_cache_namespace,
            )
            try:
                plan = plan_type.model_validate_json(response.output_text)
            except Exception as exc:
                self._record_output_failure("PLAN_VALIDATION", exc)
                raise
            self._plan_cache[request.request_id] = plan

        outline = None
        if requires_package_outline(request):
            outline_content = self._with_instruction(
                base_content,
                PACKAGE_OUTLINE_INSTRUCTION
                + f"\nPurpose: {request.purpose}\nPhase: {request.phase.value}",
            )
            self._egress_gateway.manifest(outline_content)
            outline_content = outline_content + ({
                "type": "input_text",
                "text": "PRIOR_DECOMPOSED_DECISION_PLAN\n" + plan.model_dump_json(),
            },)
            outline_schema = _openai_strict_schema(
                TerraPackageOutline.model_json_schema(mode="validation")
            )
            await self._ensure_source_cache(
                "package-outline",
                outline_content,
                request,
            )
            response = await self._create_response(
                request=request,
                content=outline_content,
                schema=outline_schema,
                schema_name="terra_package_outline_v1",
                stage="ANALYSIS",
                max_output_tokens=TERRA_PACKAGE_OUTLINE_MAX_OUTPUT_TOKENS,
                cache_namespace="package-outline",
            )
            try:
                outline = TerraPackageOutline.model_validate_json(response.output_text)
            except Exception as exc:
                self._record_output_failure("OUTLINE_VALIDATION", exc)
                raise
        try:
            return assemble_analyzer_result(request, envelope, plan, outline)
        except Exception as exc:
            self._record_output_failure("LOCAL_ASSEMBLY", exc)
            raise

    @staticmethod
    def _with_instruction(content, instruction):
        blocks = [dict(value) for value in content]
        blocks[0]["text"] = "INSTRUCTIONS_AND_PHASE\n" + instruction
        return tuple(blocks)

    def _record_output_failure(self, stage_name: str, exc: Exception) -> None:
        previous = self._request_diagnostics[-1] if self._request_diagnostics else None
        self._request_diagnostics.append(TerraRequestDiagnostic(
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
        ))

    async def _ensure_source_cache(self, cache_name, content, request) -> None:
        async with self._cache_lock:
            if cache_name in self._warmed_prefixes:
                return
            schema = _openai_strict_schema(
                TerraCacheWarmResult.model_json_schema(mode="validation")
            )
            await self._create_response(
                request=request,
                content=content[:4] + ({
                    "type": "input_text",
                    "text": "CACHE_WARMUP_CONTEXT\nReturn {\"status\":\"ready\"} only.",
                },),
                schema=schema,
                schema_name="terra_cache_warm_v1",
                max_output_tokens=1024,
                stage="CACHE_WARM",
                cache_namespace=cache_name,
            )
            self._warmed_prefixes.add(cache_name)

    async def _create_response(
        self,
        *,
        request,
        content,
        schema,
        schema_name: str = "analyzer_turn_result_v1",
        stage: TerraRequestStage,
        max_output_tokens: int | None = None,
        cache_namespace: str = "shared",
    ):
        client_request_id = f"specops-workshop-{stage.lower()}-{uuid4()}"
        arguments = {
            "model": self._model,
            "reasoning": {"effort": request.effort},
            "store": False,
            "extra_headers": {"X-Client-Request-Id": client_request_id},
            "prompt_cache_key": f"{TERRA_PROMPT_CACHE_KEY}-{cache_namespace}",
            "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
            "input": [{
                "role": "user",
                "content": list(content),
            }],
            "text": {"format": {
                "type": "json_schema", "name": schema_name, "strict": True,
                "schema": schema,
            }},
        }
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
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
            self._request_diagnostics.append(TerraRequestDiagnostic(
                stage=stage,
                outcome="FAIL",
                client_request_id=client_request_id,
                provider_request_id=provider_request_id,
                status_code=status_code,
                error_type=type(exc).__name__,
            ))
            raise TerraProviderRequestError(
                stage=stage,
                client_request_id=client_request_id,
                provider_request_id=provider_request_id,
                status_code=status_code,
                request_diagnostics=self.request_diagnostics,
            ) from exc
        self._request_diagnostics.append(TerraRequestDiagnostic(
            stage=stage,
            outcome="PASS",
            client_request_id=client_request_id,
            provider_request_id=_safe_request_identifier(
                getattr(response, "_request_id", None)
            ),
            status_code=200,
            error_type=None,
        ))
        return response
