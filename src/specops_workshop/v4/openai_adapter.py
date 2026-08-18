"""OpenAI Responses adapter for the stored V4 Workshop context.

This module has no model fallback and no local semantic search.  It uploads the
two approved source documents once, binds every stored response to one explicit
Conversation, uses the narrow operation schema, and returns only locally
validated candidates.  Provider diagnostics are deliberately content-free.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from specops_workflow.spec_identity_materialization import (
    SPEC_ANALYZER_COLLECTIONS,
    SPEC_EVIDENCE_CLAIM_TEXT_FIELDS,
    spec_collection_slot_assignments,
)

from . import contracts
from .schema_compiler import (
    compile_openai_strict_payload_schema,
    native_schema_for,
    validate_openai_strict_schema,
)


PROVIDER_IO_TIMEOUT_SECONDS = 60.0
SLOW_RESPONSE_OBSERVATION_SECONDS = 60.0
BACKGROUND_POLL_SECONDS = 1.0
MAX_OUTPUT_TOKENS = {
    contracts.AnalyzerOperation.BOOTSTRAP: 24_000,
    contracts.AnalyzerOperation.TURN_ANALYSIS: 20_000,
    contracts.AnalyzerOperation.GUIDANCE: 8_000,
    contracts.AnalyzerOperation.REVIEW_NARRATION: 8_000,
    contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS: 128_000,
    contracts.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS: 24_000,
}
ARTIFACT_PAYLOAD_SCHEMAS = {
    contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS: (
        "spec-package-payload.schema.json",
        "https://example.local/schemas/spec-package-payload.schema.json",
    ),
    contracts.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS: (
        "technical-contract-payload.schema.json",
        "https://example.local/schemas/technical-contract-payload.schema.json",
    ),
}

@dataclass(frozen=True)
class ProviderSourceUpload:
    source: contracts.SourceIdentity
    content: bytes


@dataclass(frozen=True)
class PreparedProviderContext:
    provider_conversation_id: str
    source_set: contracts.SourceSetBinding


@dataclass(frozen=True)
class BootstrapResult:
    context: contracts.AnalyzerContextBinding
    candidate: contracts.InterviewBriefCandidate


@dataclass(frozen=True)
class ProviderLifecycleEvent:
    """Content-free correlation for one logical provider request."""

    event: str
    operation: str
    client_request_id: str
    response_id: str | None
    provider_request_id: str | None
    status: str | None
    incomplete_reason: Literal["max_output_tokens", "content_filter"] | None
    duration_ms: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    submitted_max_output_tokens: int | None
    response_max_output_tokens: int | None
    strict_schema_shape_sha256: str | None
    strict_schema_bytes: int | None
    output_item_count: int | None
    output_text_present: bool | None
    output_text_character_count: int | None


@dataclass(frozen=True)
class ProviderResourceDeletion:
    """Content-free terminal observation for one provider resource DELETE."""

    resource_kind: Literal["RESPONSE", "CONVERSATION", "FILE"]
    resource_id: str
    client_request_id: str
    provider_request_id: str | None
    outcome: Literal["DELETED", "ALREADY_ABSENT", "UNCONFIRMED", "CANCELLED"]
    safe_error_code: str | None
    retryable: bool
    duration_ms: int

    @property
    def confirmed_absent(self) -> bool:
        return self.outcome in {"DELETED", "ALREADY_ABSENT"}


@dataclass(frozen=True)
class ProviderCleanupReceipt:
    """Truthful aggregate; incomplete cleanup never masquerades as success."""

    deletions: tuple[ProviderResourceDeletion, ...]

    @property
    def completed(self) -> bool:
        return all(item.confirmed_absent for item in self.deletions)

    @property
    def retryable(self) -> bool:
        return any(not item.confirmed_absent and item.retryable for item in self.deletions)


class ProviderAdapterError(RuntimeError):
    """A provider failure whose only public detail is a safe receipt."""

    def __init__(
        self,
        receipt: contracts.ProviderFailureReceipt,
        *,
        bootstrap_correction_code: str | None = None,
    ) -> None:
        super().__init__(receipt.code.value)
        self.receipt = receipt
        self.bootstrap_correction_code = bootstrap_correction_code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_provider_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 512 or not value.isascii():
        return None
    if not all(character.isalnum() or character in "._:-" for character in value):
        return None
    return value


def _safe_token_count(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _safe_incomplete_reason(
    response: Any | None,
) -> Literal["max_output_tokens", "content_filter"] | None:
    reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
    if reason == "max_output_tokens":
        return "max_output_tokens"
    if reason == "content_filter":
        return "content_filter"
    return None


_REDACTED_SCHEMA_VALUE_KEYS = frozenset(
    {"const", "default", "description", "enum", "examples", "title"}
)


def _schema_shape(value: Any, *, parent_key: str | None = None) -> Any:
    """Project a JSON Schema to structure without retaining scalar text values."""

    if parent_key in _REDACTED_SCHEMA_VALUE_KEYS:
        if isinstance(value, list):
            return {
                "kind": "list",
                "length": len(value),
                "item_kinds": sorted({type(item).__name__ for item in value}),
            }
        return {"kind": type(value).__name__}
    if isinstance(value, dict):
        return {
            key: _schema_shape(item, parent_key=key)
            for key, item in sorted(value.items())
            if isinstance(key, str)
        }
    if isinstance(value, list):
        return [_schema_shape(item) for item in value]
    if isinstance(value, str):
        return "<string>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"


def _safe_request_envelope(
    request_arguments: dict[str, Any] | None,
) -> tuple[int | None, str | None, int | None]:
    if request_arguments is None:
        return None, None, None
    submitted_max_output_tokens = _safe_token_count(
        request_arguments.get("max_output_tokens")
    )
    text = request_arguments.get("text")
    output_format = text.get("format") if isinstance(text, dict) else None
    schema = output_format.get("schema") if isinstance(output_format, dict) else None
    if not isinstance(schema, dict):
        return submitted_max_output_tokens, None, None
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    shape = json.dumps(
        _schema_shape(schema), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return submitted_max_output_tokens, hashlib.sha256(shape).hexdigest(), len(encoded)


def _safe_output_shape(
    response: Any | None,
) -> tuple[int | None, bool | None, int | None]:
    if response is None:
        return None, None, None
    output = getattr(response, "output", None)
    output_item_count = len(output) if isinstance(output, (list, tuple)) else None
    output_text = getattr(response, "output_text", None)
    if not isinstance(output_text, str):
        return output_item_count, None, None
    return output_item_count, bool(output_text), len(output_text)


_KNOWN_SCHEMA_PATHS = frozenset(
    {
        "protocol_version",
        "output_type",
        "analyzer_run_id",
        "context_id",
        "request_hash",
        "source_set_hash",
        "based_on_case_revision",
        "transcript_event_id",
        "disposition",
        "no_change_reason_code",
        "evidence_candidates",
        "new_problems",
        "new_problem_clusters",
        "revised_problem_clusters",
        "new_questions",
        "revised_questions",
        "low_risk_facts",
        "decisions",
        "problem_assessments",
        "evidence_findings",
        "customer_promise_summary",
        "problems",
        "problem_clusters",
        "questions",
        "initial_runway",
        "confirmation_checkpoints",
        "recommended_question",
        "safe_alternates",
        "do_not_ask_question_refs",
        "dependencies",
        "acknowledgement_suggestion",
        "decision_batch_view_id",
        "decision_batch_view_hash",
        "spoken_opening",
        "items",
        "spoken_confirmation_question",
        "foundation_artifact_id",
        "identity_plan_id",
        "identity_plan_version",
        "semantic_state_hash",
        "candidate_payload_json",
        "payload_schema_id",
        "payload_schema_version",
    }
)

_SAFE_ROOT_INVARIANT_PATHS = {
    "candidate keys must be unique across the brief": (
        "graph",
        "candidate_key_uniqueness",
    ),
    "problem references unknown evidence candidate": (
        "graph",
        "problem_evidence_reference",
    ),
    "cluster references unknown problem": (
        "graph",
        "cluster_problem_reference",
    ),
    "question references unknown problem": (
        "graph",
        "question_problem_reference",
    ),
    "runway references unknown question": (
        "graph",
        "runway_question_reference",
    ),
    "checkpoint references unknown cluster": (
        "graph",
        "checkpoint_cluster_reference",
    ),
}


def safe_validation_diagnostics(error: Exception) -> tuple[contracts.SafeValidationDiagnostic, ...]:
    if not isinstance(error, ValidationError):
        return ()
    result: list[contracts.SafeValidationDiagnostic] = []
    for item in error.errors(include_url=False, include_context=True, include_input=False):
        location = item.get("loc", ())
        context_error = item.get("ctx", {}).get("error")
        safe_root_path = (
            _SAFE_ROOT_INVARIANT_PATHS.get(str(context_error))
            if not location
            else None
        )
        path: list[str] = list(safe_root_path or ())
        if safe_root_path is None:
            for segment in location[:16]:
                if isinstance(segment, int) and 0 <= segment <= 9_999:
                    path.append(str(segment))
                elif isinstance(segment, str) and segment in _KNOWN_SCHEMA_PATHS:
                    path.append(segment)
                elif isinstance(segment, str):
                    path.append("$unknown")
                    break
                else:
                    path.append("$unknown")
                    break
        raw_code = str(item.get("type", ""))
        if raw_code == "missing":
            code = contracts.SafeValidationCode.MISSING
        elif raw_code == "extra_forbidden":
            code = contracts.SafeValidationCode.UNKNOWN_FIELD
        elif raw_code.endswith("_type") or raw_code in {"model_attributes_type", "dict_type"}:
            code = contracts.SafeValidationCode.INVALID_TYPE
        elif "discriminator" in raw_code or raw_code == "union_tag_invalid":
            code = contracts.SafeValidationCode.WRONG_DISCRIMINATOR
        elif any(token in raw_code for token in ("less_than", "greater_than", "too_short", "too_long")):
            code = contracts.SafeValidationCode.OUT_OF_RANGE
        elif raw_code == "json_invalid":
            code = contracts.SafeValidationCode.MALFORMED_JSON
        else:
            code = contracts.SafeValidationCode.INVARIANT_FAILED
        diagnostic = contracts.SafeValidationDiagnostic(path=tuple(path), code=code)
        if diagnostic not in result:
            result.append(diagnostic)
        if len(result) == 50:
            break
    return tuple(result)


_SAFE_PROVIDER_SCHEMA_TERMS = {
    "additionalProperties": "additional_properties",
    "allOf": "all_of",
    "anyOf": "any_of",
    "const": "const",
    "contains": "contains",
    "dependentRequired": "dependent_required",
    "dependentSchemas": "dependent_schemas",
    "enum": "enum",
    "exclusiveMaximum": "exclusive_maximum",
    "exclusiveMinimum": "exclusive_minimum",
    "format": "format",
    "items": "items",
    "maxContains": "max_contains",
    "maxItems": "max_items",
    "maxLength": "max_length",
    "maximum": "maximum",
    "minContains": "min_contains",
    "minItems": "min_items",
    "minLength": "min_length",
    "minimum": "minimum",
    "multipleOf": "multiple_of",
    "not": "not",
    "oneOf": "one_of",
    "pattern": "pattern",
    "patternProperties": "pattern_properties",
    "propertyNames": "property_names",
    "required": "required",
    "type": "type",
    "unevaluatedProperties": "unevaluated_properties",
    "uniqueItems": "unique_items",
    "$defs": "defs",
    "$ref": "ref",
}


def safe_provider_schema_diagnostics(
    error: Exception,
) -> tuple[contracts.SafeValidationDiagnostic, ...]:
    """Extract only allowlisted JSON-Schema terms from a provider 400 body."""

    body = getattr(error, "body", None)
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str) or len(message) > 20_000:
        return ()
    return tuple(
        contracts.SafeValidationDiagnostic(
            path=("provider_schema", safe_term),
            code=contracts.SafeValidationCode.INVARIANT_FAILED,
        )
        for provider_term, safe_term in _SAFE_PROVIDER_SCHEMA_TERMS.items()
        if any(
            f"{quote}{provider_term}{quote}" in message
            for quote in ("'", '"', "`")
        )
    )[:16]


class StoredConversationOpenAIAdapter:
    """Sole V4 production provider adapter (Terra, medium, stored Conversation)."""

    def __init__(
        self,
        *,
        api_key: str,
        client: Any | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=PROVIDER_IO_TIMEOUT_SECONDS,
        )
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._logger = logger or logging.getLogger("specops.workshop.provider")
        self._failures: deque[contracts.ProviderFailureReceipt] = deque(maxlen=32)
        self._lifecycle_events: deque[ProviderLifecycleEvent] = deque(maxlen=128)
        self._cleanup_events: deque[ProviderResourceDeletion] = deque(maxlen=128)
        self._background_responses: dict[str, Any] = {}
        self._background_started_at: dict[str, float] = {}

    @property
    def failure_receipts(self) -> tuple[contracts.ProviderFailureReceipt, ...]:
        return tuple(self._failures)

    @property
    def lifecycle_events(self) -> tuple[ProviderLifecycleEvent, ...]:
        return tuple(self._lifecycle_events)

    @property
    def cleanup_events(self) -> tuple[ProviderResourceDeletion, ...]:
        return tuple(self._cleanup_events)

    def _record_cleanup_event(self, value: ProviderResourceDeletion) -> None:
        self._cleanup_events.append(value)
        self._logger.info(
            json.dumps(value.__dict__, sort_keys=True, separators=(",", ":"))
        )

    async def prepare_context(
        self,
        sources: tuple[ProviderSourceUpload, ProviderSourceUpload],
    ) -> PreparedProviderContext:
        roles = tuple(item.source.role for item in sources)
        if roles != (contracts.SourceRole.PM_SPEC, contracts.SourceRole.TECHNICAL_CONTRACT):
            raise ValueError("provider sources must be ordered PM_SPEC then TECHNICAL_CONTRACT")
        if len({item.source.source_id for item in sources}) != 2:
            raise ValueError("provider sources must have distinct Foundation identities")

        uploaded: list[str] = []
        try:
            for item in sources:
                uploaded.append(await self.upload_source(item))
            conversation_id = await self.create_conversation(sources)
        except Exception:
            await self._release_prepared_ids(
                None, tuple(uploaded)
            )
            raise
        return self.prepared_from_ids(sources, tuple(uploaded), conversation_id)

    async def upload_source(self, item: ProviderSourceUpload) -> str:
        """Upload one ordered source under a stable logical request identity."""

        actual_hash = "sha256:" + hashlib.sha256(item.content).hexdigest()
        if actual_hash != item.source.payload_hash:
            raise ValueError("provider source bytes do not match the Foundation payload hash")
        client_request_id = f"specops-file-{item.source.source_id}"
        try:
            provider_file = await asyncio.wait_for(
                self._client.files.create(
                    file=(item.source.filename, item.content, item.source.media_type),
                    purpose="user_data",
                    extra_headers={"X-Client-Request-Id": client_request_id},
                ),
                timeout=PROVIDER_IO_TIMEOUT_SECONDS,
            )
            file_id = _safe_provider_identifier(getattr(provider_file, "id", None))
            if file_id is None:
                raise ValueError("unsafe provider file identifier")
            return file_id
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage.FILE_UPLOAD,
                client_request_id=client_request_id,
            ) from exc

    async def create_conversation(
        self, sources: tuple[ProviderSourceUpload, ProviderSourceUpload]
    ) -> str:
        """Create the one stored Conversation under a stable logical identity."""

        conversation_request_id = "specops-conversation-" + hashlib.sha256(
            "|".join(str(item.source.source_id) for item in sources).encode("ascii")
        ).hexdigest()[:32]
        try:
            conversation = await asyncio.wait_for(
                self._client.conversations.create(
                    metadata={"protocol": contracts.PROTOCOL_VERSION},
                    extra_headers={"X-Client-Request-Id": conversation_request_id},
                ),
                timeout=PROVIDER_IO_TIMEOUT_SECONDS,
            )
            conversation_id = _safe_provider_identifier(getattr(conversation, "id", None))
            if conversation_id is None:
                raise ValueError("unsafe provider Conversation identifier")
            return conversation_id
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage.CONVERSATION_CREATE,
                client_request_id=conversation_request_id,
            ) from exc

    def prepared_from_ids(
        self,
        sources: tuple[ProviderSourceUpload, ProviderSourceUpload],
        file_ids: tuple[str, str],
        conversation_id: str,
    ) -> PreparedProviderContext:
        if len(file_ids) != 2:
            raise ValueError("exactly two provider file IDs are required")
        return PreparedProviderContext(
            provider_conversation_id=conversation_id,
            source_set=contracts.SourceSetBinding(
                source_set_hash=self.source_set_hash(tuple(item.source for item in sources)),
                ordered_sources=tuple(
                    contracts.ProviderSourceBinding(source=item.source, provider_file_id=file_id)
                    for item, file_id in zip(sources, file_ids, strict=True)
                ),
            ),
        )

    async def release_resource_ids(
        self,
        conversation_id: str | None,
        file_ids: tuple[str, ...],
        *,
        response_id: str | None = None,
        response_ids: tuple[str, ...] = (),
    ) -> ProviderCleanupReceipt:
        bootstrap_response_ids = () if response_id is None else (response_id,)
        tracked_response_ids = tuple(
            dict.fromkeys((*bootstrap_response_ids, *response_ids))
        )
        return await self._release_prepared_ids(
            conversation_id, file_ids, response_ids=tracked_response_ids
        )

    async def bootstrap(
        self,
        request: contracts.BootstrapAnalyzerRequest,
        *,
        prepared: PreparedProviderContext,
        session_id: UUID,
    ) -> BootstrapResult:
        response_id = await self.start_bootstrap(request, prepared=prepared)
        return await self.finish_bootstrap(
            request,
            prepared=prepared,
            session_id=session_id,
            response_id=response_id,
        )

    async def start_bootstrap(
        self,
        request: contracts.BootstrapAnalyzerRequest,
        *,
        prepared: PreparedProviderContext,
    ) -> str:
        """Create one stored background Response and return its durable identity."""

        return await self._start_bootstrap(
            request, prepared=prepared, grounding_correction=False
        )

    async def start_bootstrap_grounding_correction(
        self,
        request: contracts.BootstrapAnalyzerRequest,
        *,
        prepared: PreparedProviderContext,
    ) -> str:
        """Create the sole correction after a completed candidate failed source preflight."""

        return await self._start_bootstrap(
            request, prepared=prepared, grounding_correction=True
        )

    async def start_bootstrap_graph_correction(
        self,
        request: contracts.BootstrapAnalyzerRequest,
        *,
        prepared: PreparedProviderContext,
    ) -> str:
        """Create the sole correction after a known question-reference failure."""

        return await self._start_bootstrap(
            request, prepared=prepared, graph_correction=True
        )

    async def _start_bootstrap(
        self,
        request: contracts.BootstrapAnalyzerRequest,
        *,
        prepared: PreparedProviderContext,
        grounding_correction: bool = False,
        graph_correction: bool = False,
    ) -> str:
        """Create one background Response; callers durably checkpoint the returned ID."""

        if request.provider_conversation_id != prepared.provider_conversation_id:
            raise ValueError("bootstrap request does not bind the prepared Conversation")
        if request.source_set != prepared.source_set:
            raise ValueError("bootstrap request does not bind the two uploaded sources")
        operation = contracts.AnalyzerOperation.BOOTSTRAP
        if grounding_correction and graph_correction:
            raise ValueError("bootstrap correction reason must be singular")
        arguments = self._response_arguments(
            request,
            bootstrap=not grounding_correction and not graph_correction,
            grounding_correction=grounding_correction,
            graph_correction=graph_correction,
        )
        arguments["background"] = True
        started_at = self._monotonic()
        self._emit_lifecycle(
            event="provider_request.started",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            request_arguments=arguments,
        )
        try:
            response = await asyncio.wait_for(
                self._client.responses.create(**arguments),
                timeout=PROVIDER_IO_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            self._emit_lifecycle(
                event="provider_request.cancelled",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                status="CREATE_CANCELLED",
                request_arguments=arguments,
            )
            raise
        except TimeoutError as exc:
            self._emit_lifecycle(
                event="provider_request.timeout",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                status="CREATE_ID_UNCERTAIN",
                request_arguments=arguments,
            )
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage.BOOTSTRAP,
                client_request_id=request.client_request_id,
            ) from exc
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage.BOOTSTRAP,
                client_request_id=request.client_request_id,
            ) from exc
        response_id = _safe_provider_identifier(getattr(response, "id", None))
        if response_id is None:
            raise self._output_error(request.client_request_id, ValueError("unsafe response ID"))
        self._background_responses[response_id] = response
        self._background_started_at[response_id] = started_at
        self._emit_lifecycle(
            event="provider_request.accepted",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            response=response,
            request_arguments=arguments,
        )
        return response_id

    async def finish_bootstrap(
        self,
        request: contracts.BootstrapAnalyzerRequest,
        *,
        prepared: PreparedProviderContext,
        session_id: UUID,
        response_id: str,
    ) -> BootstrapResult:
        """Poll the known Response; never create a replacement after uncertainty."""

        if request.provider_conversation_id != prepared.provider_conversation_id:
            raise ValueError("bootstrap request does not bind the prepared Conversation")
        if request.source_set != prepared.source_set:
            raise ValueError("bootstrap request does not bind the two uploaded sources")
        if _safe_provider_identifier(response_id) != response_id:
            raise ValueError("unsafe stored bootstrap response identifier")
        operation = contracts.AnalyzerOperation.BOOTSTRAP
        started_at = self._background_started_at.setdefault(response_id, self._monotonic())
        request_arguments = self._response_arguments(request, bootstrap=True)
        request_arguments["background"] = True
        response = self._background_responses.get(response_id)
        slow_observed = False
        try:
            while True:
                if response is None:
                    response = await asyncio.wait_for(
                        self._client.responses.retrieve(response_id),
                        timeout=PROVIDER_IO_TIMEOUT_SECONDS,
                    )
                returned_id = _safe_provider_identifier(getattr(response, "id", None))
                if returned_id != response_id:
                    raise ValueError("retrieved Response identity changed")
                self._background_responses[response_id] = response
                status = getattr(response, "status", None) or "completed"
                if (
                    not slow_observed
                    and self._monotonic() - started_at
                    >= SLOW_RESPONSE_OBSERVATION_SECONDS
                ):
                    slow_observed = True
                    self._emit_lifecycle(
                        event="provider_request.timeout",
                        operation=operation,
                        client_request_id=request.client_request_id,
                        started_at=started_at,
                        response=response,
                        request_arguments=request_arguments,
                    )
                if status not in {"queued", "in_progress"}:
                    break
                await self._sleep(BACKGROUND_POLL_SECONDS)
                response = None
        except asyncio.CancelledError:
            self._emit_lifecycle(
                event="provider_request.cancelled",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                response=self._background_responses.get(response_id),
                request_arguments=request_arguments,
            )
            raise
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage.BOOTSTRAP,
                client_request_id=request.client_request_id,
            ) from exc

        self._emit_lifecycle(
            event="provider_request.completed",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            response=response,
            request_arguments=request_arguments,
        )
        if status != "completed":
            raise self._provider_error(
                RuntimeError(f"safe terminal response status: {status}"),
                stage=contracts.ProviderProcessingStage.BOOTSTRAP,
                client_request_id=request.client_request_id,
            )
        try:
            candidate = contracts.InterviewBriefCandidate.model_validate_json(
                response.output_text
            )
            self._validate_candidate_echo(request, candidate)
        except Exception as exc:
            raise self._output_error(
                request.client_request_id,
                exc,
                provider_request_id=_safe_provider_identifier(
                    getattr(response, "_request_id", None)
                ),
            ) from exc
        self._background_responses.pop(response_id, None)
        self._background_started_at.pop(response_id, None)
        assert isinstance(candidate, contracts.InterviewBriefCandidate)
        context = contracts.AnalyzerContextBinding(
            protocol_version=contracts.PROTOCOL_VERSION,
            context_id=request.context_id,
            session_id=session_id,
            provider=contracts.ProviderName.OPENAI,
            provider_conversation_id=prepared.provider_conversation_id,
            bootstrap_response_id=response_id,
            model="gpt-5.6-terra",
            reasoning_effort=contracts.ReasoningEffort.MEDIUM,
            conversation_state_persisted=True,
            response_store_enabled=True,
            analyzer_contract=request.analyzer_contract,
            source_set=prepared.source_set,
            status=contracts.ContextStatus.ACTIVE,
            created_at=self._now(),
            invalidated_at=None,
            invalidation_reason=None,
        )
        return BootstrapResult(context=context, candidate=candidate)

    async def execute(
        self,
        request: contracts.AnalyzerProviderRequest,
        *,
        context: contracts.AnalyzerContextBinding,
    ) -> contracts.AnalyzerProviderCandidate:
        if request.request_type is contracts.AnalyzerOperation.BOOTSTRAP:
            raise ValueError("use bootstrap() for the initial provider request")
        self._validate_context(request, context)
        candidate, _ = await self._execute(request, bootstrap=False)
        return candidate

    async def execute_with_response_checkpoint(
        self,
        request: contracts.AnalyzerProviderRequest,
        *,
        context: contracts.AnalyzerContextBinding,
        checkpoint: Callable[[str], None],
    ) -> contracts.AnalyzerProviderCandidate:
        """Checkpoint a stored Response ID before parsing provider output."""

        if request.request_type is contracts.AnalyzerOperation.BOOTSTRAP:
            raise ValueError("use bootstrap() for the initial provider request")
        self._validate_context(request, context)
        candidate, _ = await self._execute(
            request,
            bootstrap=False,
            response_checkpoint=checkpoint,
        )
        return candidate

    async def execute_turn_analysis_correction_with_response_checkpoint(
        self,
        request: contracts.AnalyzeFinalTurnRequest,
        *,
        context: contracts.AnalyzerContextBinding,
        quarantined_candidate_keys: tuple[str, ...],
        checkpoint: Callable[[str], None],
    ) -> contracts.TurnAnalysisCandidate:
        """Create exactly one corrective TURN_ANALYSIS for a quarantined branch."""

        self._validate_context(request, context)
        if not quarantined_candidate_keys:
            raise ValueError("turn correction requires a quarantined dependency closure")
        candidate, _ = await self._execute(
            request,
            bootstrap=False,
            response_checkpoint=checkpoint,
            turn_correction_keys=quarantined_candidate_keys,
        )
        assert isinstance(candidate, contracts.TurnAnalysisCandidate)
        return candidate

    async def resume_stored_response(
        self,
        request: contracts.AnalyzerProviderRequest,
        *,
        context: contracts.AnalyzerContextBinding,
        response_id: str,
    ) -> contracts.AnalyzerProviderCandidate:
        """Resume one known stored Response; never create a replacement."""

        self._validate_context(request, context)
        if _safe_provider_identifier(response_id) != response_id:
            raise ValueError("unsafe stored Response identifier")
        operation = contracts.AnalyzerOperation(request.request_type)
        _, _, candidate_type = native_schema_for(operation)
        started_at = self._monotonic()
        self._emit_lifecycle(
            event="provider_request.started",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            status="RESUME_KNOWN_RESPONSE",
        )
        response = None
        status = "queued"
        slow_observed = False
        try:
            while status in {"queued", "in_progress"}:
                response = await asyncio.wait_for(
                    self._client.responses.retrieve(
                        response_id,
                        extra_headers={
                            "X-Client-Request-Id": request.client_request_id
                        },
                    ),
                    timeout=PROVIDER_IO_TIMEOUT_SECONDS,
                )
                if _safe_provider_identifier(getattr(response, "id", None)) != response_id:
                    raise ValueError("retrieved Response identity changed")
                status = getattr(response, "status", None) or "completed"
                if (
                    not slow_observed
                    and self._monotonic() - started_at
                    >= SLOW_RESPONSE_OBSERVATION_SECONDS
                ):
                    slow_observed = True
                    self._emit_lifecycle(
                        event="provider_request.timeout",
                        operation=operation,
                        client_request_id=request.client_request_id,
                        started_at=started_at,
                        response=response,
                    )
                if status in {"queued", "in_progress"}:
                    await self._sleep(BACKGROUND_POLL_SECONDS)
        except asyncio.CancelledError:
            self._emit_lifecycle(
                event="provider_request.cancelled",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                response=response,
            )
            raise
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage(operation.value),
                client_request_id=request.client_request_id,
            ) from exc
        assert response is not None
        self._emit_lifecycle(
            event="provider_request.completed",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            response=response,
        )
        if status != "completed":
            raise self._provider_error(
                RuntimeError(f"safe terminal response status: {status}"),
                stage=contracts.ProviderProcessingStage(operation.value),
                client_request_id=request.client_request_id,
            )
        try:
            candidate = self._candidate_from_provider_output(
                operation, candidate_type, response.output_text
            )
            self._validate_candidate_echo(request, candidate)
        except Exception as exc:
            raise self._output_error(
                request.client_request_id,
                exc,
                provider_request_id=_safe_provider_identifier(
                    getattr(response, "_request_id", None)
                ),
            ) from exc
        return candidate

    async def context_is_available(self, context: contracts.AnalyzerContextBinding) -> bool:
        if context.status is not contracts.ContextStatus.ACTIVE:
            return False
        client_request_id = f"specops-context-check-{context.context_id}"
        try:
            value = await asyncio.wait_for(
                self._client.conversations.retrieve(
                    context.provider_conversation_id,
                    extra_headers={"X-Client-Request-Id": client_request_id},
                ),
                timeout=PROVIDER_IO_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage.CONVERSATION_CREATE,
                client_request_id=client_request_id,
            )
            return False
        return getattr(value, "id", None) == context.provider_conversation_id

    async def release_context(
        self, context: contracts.AnalyzerContextBinding
    ) -> ProviderCleanupReceipt:
        """Best-effort provider cleanup after Foundation has invalidated a binding.

        Foundation invalidation is authoritative and happens first. Cleanup is
        bounded and idempotent; no source content or provider error text is
        retained when a remote object has already disappeared.
        """

        return await self._release_prepared_ids(
            context.provider_conversation_id,
            tuple(item.provider_file_id for item in context.source_set.ordered_sources),
            response_ids=(context.bootstrap_response_id,),
        )

    async def release_prepared(
        self, prepared: PreparedProviderContext
    ) -> ProviderCleanupReceipt:
        return await self._release_prepared_ids(
            prepared.provider_conversation_id,
            tuple(item.provider_file_id for item in prepared.source_set.ordered_sources),
        )

    async def _release_prepared_ids(
        self,
        conversation_id: str | None,
        file_ids: tuple[str, ...],
        *,
        response_ids: tuple[str, ...] = (),
    ) -> ProviderCleanupReceipt:
        async def bounded(
            resource: Any,
            identifier: str,
            request_id: str,
            resource_kind: Literal["RESPONSE", "CONVERSATION", "FILE"],
        ) -> ProviderResourceDeletion:
            started_at = self._monotonic()
            delete = getattr(resource, "delete", None)
            if delete is None:
                result = ProviderResourceDeletion(
                    resource_kind=resource_kind,
                    resource_id=identifier,
                    client_request_id=request_id,
                    provider_request_id=None,
                    outcome="UNCONFIRMED",
                    safe_error_code="DELETE_UNSUPPORTED",
                    retryable=False,
                    duration_ms=max(0, int((self._monotonic() - started_at) * 1000)),
                )
                self._record_cleanup_event(result)
                return result
            try:
                response = await asyncio.wait_for(
                    delete(
                        identifier,
                        extra_headers={"X-Client-Request-Id": request_id},
                    ),
                    timeout=PROVIDER_IO_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                result = ProviderResourceDeletion(
                    resource_kind=resource_kind,
                    resource_id=identifier,
                    client_request_id=request_id,
                    provider_request_id=None,
                    outcome="CANCELLED",
                    safe_error_code="CANCELLED",
                    retryable=True,
                    duration_ms=max(0, int((self._monotonic() - started_at) * 1000)),
                )
                self._record_cleanup_event(result)
                raise
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                provider_request_id = _safe_provider_identifier(
                    getattr(exc, "request_id", None)
                )
                if status == 404:
                    outcome = "ALREADY_ABSENT"
                    safe_error_code = None
                    retryable = False
                elif isinstance(exc, (TimeoutError, ConnectionError)):
                    outcome = "UNCONFIRMED"
                    safe_error_code = (
                        "DELETE_TIMEOUT" if isinstance(exc, TimeoutError) else "DELETE_CONNECTION"
                    )
                    retryable = True
                elif isinstance(status, int) and (status == 429 or status >= 500):
                    outcome = "UNCONFIRMED"
                    safe_error_code = f"DELETE_HTTP_{status}"
                    retryable = True
                elif isinstance(status, int) and 400 <= status <= 499:
                    outcome = "UNCONFIRMED"
                    safe_error_code = f"DELETE_HTTP_{status}"
                    retryable = False
                else:
                    outcome = "UNCONFIRMED"
                    safe_error_code = "DELETE_UNKNOWN_SAFE"
                    retryable = False
                result = ProviderResourceDeletion(
                    resource_kind=resource_kind,
                    resource_id=identifier,
                    client_request_id=request_id,
                    provider_request_id=provider_request_id,
                    outcome=outcome,
                    safe_error_code=safe_error_code,
                    retryable=retryable,
                    duration_ms=max(0, int((self._monotonic() - started_at) * 1000)),
                )
                self._record_cleanup_event(result)
                return result
            # AsyncOpenAI.responses.delete() casts a successful HTTP response to
            # None, so completion of that await is the Response DELETE receipt.
            # Conversation and File deletes expose deleted:true plus the ID.
            response_delete_completed = resource_kind == "RESPONSE"
            deleted = (
                response_delete_completed
                or getattr(response, "deleted", None) is True
            )
            returned_id = (
                identifier
                if response_delete_completed
                else _safe_provider_identifier(getattr(response, "id", None))
            )
            result = ProviderResourceDeletion(
                resource_kind=resource_kind,
                resource_id=identifier,
                client_request_id=request_id,
                provider_request_id=_safe_provider_identifier(
                    getattr(response, "_request_id", None)
                ),
                outcome=("DELETED" if deleted and returned_id == identifier else "UNCONFIRMED"),
                safe_error_code=(
                    None if deleted and returned_id == identifier else "DELETE_NOT_CONFIRMED"
                ),
                retryable=False,
                duration_ms=max(0, int((self._monotonic() - started_at) * 1000)),
            )
            self._record_cleanup_event(result)
            return result

        results: list[ProviderResourceDeletion] = []
        for response_id in response_ids:
            results.append(
                await bounded(
                    self._client.responses,
                    response_id,
                    f"specops-response-delete-{response_id}",
                    "RESPONSE",
                )
            )
        if conversation_id is not None:
            results.append(
                await bounded(
                    self._client.conversations,
                    conversation_id,
                    f"specops-conversation-delete-{conversation_id}",
                    "CONVERSATION",
                )
            )
        for file_id in file_ids:
            results.append(
                await bounded(
                    self._client.files,
                    file_id,
                    f"specops-file-delete-{file_id}",
                    "FILE",
                )
            )
        return ProviderCleanupReceipt(deletions=tuple(results))

    async def _execute(
        self,
        request: contracts.AnalyzerProviderRequest,
        *,
        bootstrap: bool,
        response_checkpoint: Callable[[str], None] | None = None,
        turn_correction_keys: tuple[str, ...] = (),
    ):
        operation = contracts.AnalyzerOperation(request.request_type)
        _, _, candidate_type = native_schema_for(operation)
        arguments = self._response_arguments(
            request,
            bootstrap=bootstrap,
            turn_correction_keys=turn_correction_keys,
        )
        arguments["background"] = True
        started_at = self._monotonic()
        self._emit_lifecycle(
            event="provider_request.started",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            request_arguments=arguments,
        )
        try:
            response = await asyncio.wait_for(
                self._client.responses.create(**arguments),
                timeout=PROVIDER_IO_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            self._emit_lifecycle(
                event="provider_request.cancelled",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                status="CREATE_CANCELLED",
                request_arguments=arguments,
            )
            raise
        except TimeoutError as exc:
            self._emit_lifecycle(
                event="provider_request.timeout",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                status="CREATE_ID_UNCERTAIN",
                request_arguments=arguments,
            )
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage(operation.value),
                client_request_id=request.client_request_id,
            ) from exc
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage(operation.value),
                client_request_id=request.client_request_id,
            ) from exc

        response_id = _safe_provider_identifier(getattr(response, "id", None))
        if response_id is None:
            raise self._output_error(request.client_request_id, ValueError("unsafe response ID"))
        # Provider acceptance is an external fact even if the following durable
        # local checkpoint fails. Emit the content-free identity first so an
        # operator can retrieve the exact Response instead of blindly recreating
        # the logical request after an uncertain local failure.
        self._emit_lifecycle(
            event="provider_request.accepted",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            response=response,
            request_arguments=arguments,
        )
        if response_checkpoint is not None:
            response_checkpoint(response_id)
        status = getattr(response, "status", None) or "completed"
        slow_observed = False
        try:
            while status in {"queued", "in_progress"}:
                await self._sleep(BACKGROUND_POLL_SECONDS)
                response = await asyncio.wait_for(
                    self._client.responses.retrieve(
                        response_id,
                        extra_headers={
                            "X-Client-Request-Id": request.client_request_id
                        },
                    ),
                    timeout=PROVIDER_IO_TIMEOUT_SECONDS,
                )
                if _safe_provider_identifier(getattr(response, "id", None)) != response_id:
                    raise ValueError("retrieved Response identity changed")
                status = getattr(response, "status", None) or "completed"
                if (
                    not slow_observed
                    and self._monotonic() - started_at
                    >= SLOW_RESPONSE_OBSERVATION_SECONDS
                ):
                    slow_observed = True
                    self._emit_lifecycle(
                        event="provider_request.timeout",
                        operation=operation,
                        client_request_id=request.client_request_id,
                        started_at=started_at,
                        response=response,
                        request_arguments=arguments,
                    )
        except asyncio.CancelledError:
            self._emit_lifecycle(
                event="provider_request.cancelled",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                response=response,
                status="KNOWN_RESPONSE_WAIT_CANCELLED",
                request_arguments=arguments,
            )
            raise
        except Exception as exc:
            raise self._provider_error(
                exc,
                stage=contracts.ProviderProcessingStage(operation.value),
                client_request_id=request.client_request_id,
            ) from exc

        self._emit_lifecycle(
            event="provider_request.completed",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            response=response,
            request_arguments=arguments,
        )
        if status != "completed":
            raise self._provider_error(
                RuntimeError(f"safe terminal response status: {status}"),
                stage=contracts.ProviderProcessingStage(operation.value),
                client_request_id=request.client_request_id,
            )
        try:
            candidate = self._candidate_from_provider_output(
                operation, candidate_type, response.output_text
            )
            self._validate_candidate_echo(request, candidate)
        except Exception as exc:
            raise self._output_error(
                request.client_request_id,
                exc,
                provider_request_id=_safe_provider_identifier(getattr(response, "_request_id", None)),
            ) from exc
        return candidate, response_id

    def _response_arguments(
        self,
        request: contracts.AnalyzerProviderRequest,
        *,
        bootstrap: bool,
        grounding_correction: bool = False,
        graph_correction: bool = False,
        turn_correction_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        operation = contracts.AnalyzerOperation(request.request_type)
        schema_name, schema, _ = native_schema_for(operation)
        schema = self._bind_candidate_echo_schema(request, schema)
        schema = self._bind_artifact_payload_output_schema(operation, schema)
        schema = self._bind_artifact_request_constraints(request, schema)
        content: list[dict[str, str]] = []
        if turn_correction_keys:
            if not isinstance(request, contracts.AnalyzeFinalTurnRequest):
                raise ValueError("turn grounding correction is TURN_ANALYSIS-only")
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        "The prior completed TURN_ANALYSIS contained source evidence that local "
                        "Foundation validation proved invalid. The independent verified branch is "
                        "already admitted. Return only a corrected dependency-closed branch for "
                        "the quarantined candidate keys in this JSON array: "
                        f"{json.dumps(turn_correction_keys, separators=(',', ':'))}. "
                        "Reuse only those candidate_key values for new keyed items; do not repeat "
                        "or revise the already admitted branch. Re-read the two source files in "
                        "this Conversation. Every returned evidence locator must bind exactly. "
                        "Use current Foundation refs from the request snapshot for any dependency "
                        "that is already admitted. If no quarantined proposal can be grounded, "
                        "return NO_SEMANTIC_CHANGE with OUT_OF_SCOPE rather than inventing evidence."
                    ),
                }
            )
        if grounding_correction:
            if not isinstance(request, contracts.BootstrapAnalyzerRequest):
                raise ValueError("grounding correction is BOOTSTRAP-only")
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        "The prior completed BOOTSTRAP candidate was deterministically rejected "
                        "because at least one QUOTE_SEARCH exact_quote did not occur verbatim in "
                        "its named source. Re-read the two source files already stored in this "
                        "Conversation and return one complete replacement candidate. Omit every "
                        "unverifiable evidence candidate and every proposal that depends on it."
                    ),
                }
            )
        if graph_correction:
            if not isinstance(request, contracts.BootstrapAnalyzerRequest):
                raise ValueError("graph correction is BOOTSTRAP-only")
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        "The prior completed BOOTSTRAP candidate was deterministically rejected "
                        "by local graph validation because at least one question referenced a "
                        "problem key that was not present in problems[].candidate_key. Return one "
                        "complete replacement candidate. Every addresses_problem_keys and "
                        "prerequisite_problem_keys entry must exactly match a problem candidate_key."
                    ),
                }
            )
        if bootstrap:
            assert isinstance(request, contracts.BootstrapAnalyzerRequest)
            content.extend(
                {"type": "input_file", "file_id": item.provider_file_id}
                for item in request.source_set.ordered_sources
            )
        payload_schema = self._artifact_payload_schema_text(
            operation, bound_candidate_schema=schema
        )
        if payload_schema is not None:
            content.append(
                {
                    "type": "input_text",
                    "text": (
                        "Provider-owned artifact payload JSON Schema: "
                        f"{payload_schema}"
                    ),
                }
            )
        content.append(
            {
                "type": "input_text",
                "text": json.dumps(
                    self._provider_request_projection(request),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        )
        return {
            "model": "gpt-5.6-terra",
            "reasoning": {"effort": "medium", "context": "all_turns"},
            "store": True,
            "conversation": request.provider_conversation_id,
            "extra_headers": {"X-Client-Request-Id": request.client_request_id},
            "instructions": self._instructions(operation),
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            "max_output_tokens": MAX_OUTPUT_TOKENS[operation],
        }

    @staticmethod
    def _artifact_payload_schema_text(
        operation: contracts.AnalyzerOperation,
        *,
        bound_candidate_schema: dict[str, Any] | None = None,
    ) -> str | None:
        if (
            operation is contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS
            and bound_candidate_schema is not None
        ):
            properties = bound_candidate_schema.get("properties")
            definitions = bound_candidate_schema.get("$defs")
            payload_ref = (
                properties.get("candidate_payload_json", {}).get("$ref")
                if isinstance(properties, dict)
                else None
            )
            if not isinstance(definitions, dict) or not isinstance(payload_ref, str):
                raise ValueError("bound Spec candidate schema lacks its payload root")
            root_name = payload_ref.removeprefix("#/$defs/")
            root = definitions.get(root_name)
            if not isinstance(root, dict):
                raise ValueError("bound Spec payload root is unresolved")
            required_definitions: set[str] = set()

            def collect(value: Any) -> None:
                if isinstance(value, list):
                    for item in value:
                        collect(item)
                    return
                if not isinstance(value, dict):
                    return
                reference = value.get("$ref")
                if isinstance(reference, str) and reference.startswith("#/$defs/"):
                    name = reference.removeprefix("#/$defs/")
                    if name not in required_definitions:
                        definition = definitions.get(name)
                        if not isinstance(definition, dict):
                            raise ValueError("bound Spec payload reference is unresolved")
                        required_definitions.add(name)
                        collect(definition)
                for item in value.values():
                    collect(item)

            collect(root)
            document = deepcopy(root)
            document["$defs"] = {
                name: deepcopy(definitions[name]) for name in sorted(required_definitions)
            }
            return json.dumps(document, sort_keys=True, separators=(",", ":"))
        schema = StoredConversationOpenAIAdapter._provider_owned_artifact_payload_schema(
            operation
        )
        if schema is None:
            return None
        return json.dumps(schema, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _artifact_payload_schema(
        operation: contracts.AnalyzerOperation,
    ) -> dict[str, Any] | None:
        binding = ARTIFACT_PAYLOAD_SCHEMAS.get(operation)
        if binding is None:
            return None
        filename, expected_schema_id = binding
        schema_path = Path(__file__).resolve().parent / "schemas" / filename
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if (
            not isinstance(schema, dict)
            or schema.get("$id") != expected_schema_id
            or schema.get("type") != "object"
            or schema.get("additionalProperties") is not False
        ):
            raise ValueError("artifact payload schema binding is invalid")
        return schema

    @staticmethod
    def _provider_owned_artifact_payload_schema(
        operation: contracts.AnalyzerOperation,
    ) -> dict[str, Any] | None:
        schema = StoredConversationOpenAIAdapter._artifact_payload_schema(operation)
        if schema is None or operation is not contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS:
            return schema
        projected = deepcopy(schema)
        for field in (
            "actors",
            "decisions",
            "evidence_catalog",
            "semantic_evidence_findings",
        ):
            projected["properties"].pop(field)
            projected["required"].remove(field)
        for definition in projected.get("$defs", {}).values():
            properties = definition.get("properties", {})
            required = definition.get("required", [])
            for field in ("evidence_refs", "source_evidence_refs"):
                if field in properties:
                    properties.pop(field)
                    required.remove(field)
        return projected

    @staticmethod
    def _provider_request_projection(
        request: contracts.AnalyzerProviderRequest,
    ) -> dict[str, Any]:
        """Return the single, compact semantic representation sent to the model."""

        value = request.model_dump(mode="json", exclude_none=False)
        if isinstance(request, contracts.SpecPackageSynthesisRequest):
            bindings = value.pop("confirmed_decision_bindings")
            identity_plan = value["identity_plan"]
            identity_plan.pop("planned_identities", None)
            for slot in identity_plan.get("slots", []):
                if slot.get("allocation_mode") == "NEW_ENTITY":
                    slot.pop("foundation_id", None)
            value["construction_blueprint"]["slots"] = [
                {"slot_key": slot["slot_key"], "purpose": slot["purpose"]}
                for slot in value["construction_blueprint"].get("slots", [])
            ]
            value["foundation_owned_record_refs"] = {
                "actors": sorted(
                    {
                        item["actor_ref"]
                        for item in bindings
                    }
                ),
                "decisions": [
                    {
                        "decision_id": item["decision_id"],
                        "decision_version": item["decision_version"],
                        "actor_ref": item["actor_ref"],
                    }
                    for item in bindings
                ],
            }
        return value

    @staticmethod
    def _bind_artifact_payload_output_schema(
        operation: contracts.AnalyzerOperation,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        payload_schema = (
            StoredConversationOpenAIAdapter._provider_owned_artifact_payload_schema(
                operation
            )
        )
        if payload_schema is None:
            return schema
        payload_wire_schema = compile_openai_strict_payload_schema(payload_schema)
        payload_definitions = payload_wire_schema.pop("$defs", {})
        prefix = f"{operation.value.title().replace('_', '')}Payload_"
        renamed = {name: f"{prefix}{name}" for name in payload_definitions}

        def rebase(value: Any) -> Any:
            if isinstance(value, list):
                return [rebase(item) for item in value]
            if not isinstance(value, dict):
                return value
            result = {key: rebase(item) for key, item in value.items()}
            reference = result.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference.removeprefix("#/$defs/")
                if name not in renamed:
                    raise ValueError("artifact payload schema reference is unresolved")
                result["$ref"] = f"#/$defs/{renamed[name]}"
            return result

        bound = deepcopy(schema)
        definitions = bound.setdefault("$defs", {})
        if not isinstance(definitions, dict):
            raise ValueError("candidate schema definitions must be an object")
        root_name = f"{prefix}Root"
        new_names = set(renamed.values()) | {root_name}
        if new_names.intersection(definitions):
            raise ValueError("artifact payload schema definition collides")
        definitions.update(
            {
                renamed[name]: rebase(definition)
                for name, definition in payload_definitions.items()
            }
        )
        definitions[root_name] = rebase(payload_wire_schema)
        properties = bound.get("properties")
        if not isinstance(properties, dict) or "candidate_payload_json" not in properties:
            raise ValueError("candidate schema lacks artifact payload field")
        if operation is contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS:
            properties.pop("identity_assignment_map", None)
            required = bound.get("required")
            if isinstance(required, list) and "identity_assignment_map" in required:
                required.remove("identity_assignment_map")
        properties["candidate_payload_json"] = {"$ref": f"#/$defs/{root_name}"}
        validate_openai_strict_schema(bound)
        return bound

    @staticmethod
    def _bind_artifact_request_constraints(
        request: contracts.AnalyzerProviderRequest,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Expose request-specific confirmed-decision cardinality to the provider."""

        if not isinstance(request, contracts.SpecPackageSynthesisRequest):
            return schema
        bindings = getattr(request, "confirmed_decision_bindings", ())

        bound = deepcopy(schema)
        properties = bound.get("properties")
        definitions = bound.get("$defs")
        payload_ref = (
            properties.get("candidate_payload_json", {}).get("$ref")
            if isinstance(properties, dict)
            else None
        )
        if not isinstance(definitions, dict) or not isinstance(payload_ref, str):
            raise ValueError("artifact output schema does not expose its payload root")
        payload_root = definitions.get(payload_ref.removeprefix("#/$defs/"))
        payload_properties = (
            payload_root.get("properties") if isinstance(payload_root, dict) else None
        )
        if not isinstance(payload_properties, dict):
            raise ValueError("Spec payload schema does not expose root fields")

        slot_capacity = Counter(
            item.entity_kind for item in request.construction_blueprint.slots
        )
        analyzer_capacity = Counter(
            item.entity_kind
            for item in request.construction_blueprint.slots
            if item.owner == "ANALYZER"
        )

        # Provider schemas carry construction minima only. Foundation enforces the
        # exact identity capacities and rejects any over-capacity candidate before
        # persistence, so provider-specific upper-bound support is not authoritative.
        def require_array_minimum(field: str, *, minimum: int) -> None:
            value = payload_properties.get(field)
            if not isinstance(value, dict):
                raise ValueError(f"Spec payload schema does not expose {field}")
            value["minItems"] = minimum

        direct_bounds = {
            "package_items": (1, analyzer_capacity["PACKAGE_ITEM"]),
            "glossary": (min(6, analyzer_capacity["GLOSSARY_TERM"]), analyzer_capacity["GLOSSARY_TERM"]),
            "outcomes": (1, analyzer_capacity["OUTCOME"]),
            "journeys": (1, analyzer_capacity["JOURNEY"]),
            "requirements": (analyzer_capacity["REQUIREMENT"], analyzer_capacity["REQUIREMENT"]),
            "data_rules": (min(6, analyzer_capacity["DATA_RULE"]), analyzer_capacity["DATA_RULE"]),
            "experience_states": (analyzer_capacity["EXPERIENCE_STATE"], analyzer_capacity["EXPERIENCE_STATE"]),
            "scenarios": (analyzer_capacity["SCENARIO"], analyzer_capacity["SCENARIO"]),
            "quality_attributes": (0, analyzer_capacity["QUALITY_ATTRIBUTE"]),
            "constraints": (min(2, analyzer_capacity["CONSTRAINT"]), analyzer_capacity["CONSTRAINT"]),
            "dependencies": (0, analyzer_capacity["DEPENDENCY"]),
            "risks": (0, analyzer_capacity["RISK"]),
            "open_items": (0, analyzer_capacity["OPEN_ITEM"]),
            "acceptance_checks": (analyzer_capacity["ACCEPTANCE_CHECK"], analyzer_capacity["ACCEPTANCE_CHECK"]),
        }
        for field, (minimum, maximum) in direct_bounds.items():
            if maximum:
                require_array_minimum(field, minimum=minimum)

        proposal_schema = properties.get("evidence_support_proposals")
        if not isinstance(proposal_schema, dict):
            raise ValueError("Spec candidate schema does not expose evidence proposals")
        proposal_schema["minItems"] = min(8, slot_capacity["EVIDENCE"])

        def referenced_object(field: str) -> dict[str, Any]:
            value = payload_properties.get(field)
            reference = value.get("$ref") if isinstance(value, dict) else None
            target = (
                definitions.get(reference.removeprefix("#/$defs/"))
                if isinstance(reference, str)
                else None
            )
            if not isinstance(target, dict):
                raise ValueError(f"Spec payload schema does not expose {field} object")
            return target

        scope_properties = referenced_object("scope").get("properties")
        if not isinstance(scope_properties, dict):
            raise ValueError("Spec payload scope schema is malformed")
        for field in ("in_scope", "non_goals", "boundaries"):
            value = scope_properties.get(field)
            if not isinstance(value, dict):
                raise ValueError("Spec payload scope collection is malformed")
            maximum = (
                analyzer_capacity["SCOPE_BOUNDARY"]
                if field == "boundaries"
                else analyzer_capacity["SCOPE_ITEM"]
            )
            if maximum:
                value["minItems"] = 1

        behaviour_properties = referenced_object("behaviour_contract").get("properties")
        if not isinstance(behaviour_properties, dict):
            raise ValueError("Spec behavior schema is malformed")
        for field in ("always", "ask_first", "never"):
            value = behaviour_properties.get(field)
            if not isinstance(value, dict):
                raise ValueError("Spec behavior collection is malformed")
            maximum = analyzer_capacity["BEHAVIOUR_RULE"]
            if maximum:
                value["minItems"] = 1

        assignments = {
            path: tuple(item.slot_key for item in slots)
            for path, slots in spec_collection_slot_assignments(
                request.construction_blueprint.slots
            ).items()
        }

        def collection_schema(path: tuple[str, ...]) -> dict[str, Any]:
            current = payload_root
            for index, field in enumerate(path):
                current_properties = (
                    current.get("properties") if isinstance(current, dict) else None
                )
                if not isinstance(current_properties, dict):
                    raise ValueError("Spec payload collection path is malformed")
                value = current_properties.get(field)
                if not isinstance(value, dict):
                    raise ValueError("Spec payload collection path is missing")
                if index == len(path) - 1:
                    return value
                reference = value.get("$ref")
                current = (
                    definitions.get(reference.removeprefix("#/$defs/"))
                    if isinstance(reference, str)
                    else None
                )
            raise AssertionError("empty Spec collection path")

        for path, _kind in SPEC_ANALYZER_COLLECTIONS:
            identities = assignments[path]
            if not identities:
                continue
            collection = collection_schema(path)
            item_schema = collection.get("items")
            if not isinstance(item_schema, dict):
                raise ValueError("Spec payload collection lacks an item schema")
            reference = item_schema.get("$ref")
            if isinstance(reference, str):
                source_name = reference.removeprefix("#/$defs/")
                source_definition = definitions.get(source_name)
                if not isinstance(source_definition, dict):
                    raise ValueError("Spec payload item reference is unresolved")
                body = deepcopy(source_definition)
            else:
                body = deepcopy(item_schema)
            body_properties = body.get("properties")
            body_required = body.get("required")
            if not isinstance(body_properties, dict) or not isinstance(body_required, list):
                raise ValueError("Spec payload identity-bearing item is malformed")
            if "id" not in body_properties or "id" not in body_required:
                raise ValueError("Spec payload identity-bearing item lacks an id")
            body_properties.pop("id")
            body_required.remove("id")
            body_name = "SpecPackageFoundationAssigned_" + "_".join(path)
            if body_name in definitions:
                raise ValueError("Foundation-assigned Spec body definition collides")
            definitions[body_name] = body
            collection.clear()
            collection.update(
                {
                    "type": "object",
                    "properties": {
                        identity: {
                            "anyOf": [
                                {"$ref": f"#/$defs/{body_name}"},
                                {"type": "null"},
                            ]
                        }
                        for identity in identities
                    },
                    "required": list(identities),
                    "additionalProperties": False,
                }
            )

        local_or_uuid_pattern = (
            r"^(?:[A-Z][A-Z0-9_]{1,63}:[0-9]{4}|"
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
            r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})$"
        )
        reference_keys = {
            "primary_customer",
            "beneficiary_actor_ref",
            "actor_ref",
            "from_ref",
            "to_ref",
            "source_id",
            "trigger_interface_ref",
            "failure_ref",
            "component_ref",
        }

        def localize_reference_schema(value: Any, key: str = "") -> None:
            if not isinstance(value, dict):
                return
            if key in reference_keys or key.endswith("_ref") or key.endswith("_refs"):
                reference = value.get("$ref")
                referenced = (
                    definitions.get(reference.removeprefix("#/$defs/"))
                    if isinstance(reference, str)
                    else None
                )
                shape = referenced if isinstance(referenced, dict) else value
                if shape.get("type") == "array":
                    retained_bounds = {
                        name: shape[name]
                        for name in ("minItems", "maxItems")
                        if name in shape
                    }
                    value.clear()
                    value.update(
                        {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "pattern": local_or_uuid_pattern,
                            },
                            **retained_bounds,
                        }
                    )
                    return
                nullable = any(
                    isinstance(branch, dict) and branch.get("type") == "null"
                    for branch in shape.get("anyOf", [])
                )
                localized_string = {
                    "type": "string",
                    "pattern": local_or_uuid_pattern,
                }
                value.clear()
                value.update(
                    {"anyOf": [localized_string, {"type": "null"}]}
                    if nullable
                    else localized_string
                )
                return
            properties_value = value.get("properties")
            if isinstance(properties_value, dict):
                for child_key, child in properties_value.items():
                    localize_reference_schema(child, child_key)
            items_value = value.get("items")
            if isinstance(items_value, dict):
                localize_reference_schema(items_value, key)

        localize_reference_schema(payload_root)
        for definition in definitions.values():
            localize_reference_schema(definition)

        proposal_items = proposal_schema.get("items")
        proposal_ref = (
            proposal_items.get("$ref") if isinstance(proposal_items, dict) else None
        )
        proposal_definition = (
            definitions.get(proposal_ref.removeprefix("#/$defs/"))
            if isinstance(proposal_ref, str)
            else None
        )
        proposal_properties = (
            proposal_definition.get("properties")
            if isinstance(proposal_definition, dict)
            else None
        )
        if not isinstance(proposal_properties, dict):
            raise ValueError("Spec evidence proposal schema is malformed")
        planned_by_kind: dict[str, list[str]] = {}
        for slot in request.construction_blueprint.slots:
            planned_by_kind.setdefault(slot.entity_kind, []).append(
                slot.slot_key
            )
        evidence_claim_ids = list(
            dict.fromkeys(
                identity
                for path in SPEC_EVIDENCE_CLAIM_TEXT_FIELDS
                for identity in assignments[path]
            )
        )
        evidence_claim_pointers = list(
            dict.fromkeys(
                "/" + "/".join((*path, identity, field))
                for path, fields in SPEC_EVIDENCE_CLAIM_TEXT_FIELDS.items()
                for identity in assignments[path]
                for field in fields
            )
        )
        proposal_properties["claim_ref"]["enum"] = evidence_claim_ids
        proposal_properties["claim_pointer"]["enum"] = evidence_claim_pointers
        proposal_properties["evidence_ref"]["enum"] = planned_by_kind.get(
            "EVIDENCE", []
        )
        proposal_properties["finding_ref"]["enum"] = planned_by_kind.get(
            "SEMANTIC_EVIDENCE_FINDING", []
        )

        decisions = (
            payload_properties.get("decisions")
            if isinstance(payload_properties, dict)
            else None
        )
        if decisions is None:
            validate_openai_strict_schema(bound)
            return bound
        if not isinstance(decisions, dict):
            raise ValueError("Spec payload decision projection is malformed")
        decisions["minItems"] = len(bindings)

        # A terminal synthesis request has no legitimate deferred decision slots.
        # Binding exact length and IDs keeps the structured response focused on the
        # already-confirmed Foundation set; local admission remains authoritative.
        has_open_problem = any(
            item.status is contracts.SemanticRecordStatus.OPEN
            for item in request.foundation_snapshot.problems
        )
        if not has_open_problem:
            decisions["maxItems"] = len(bindings)
            decision_ref = decisions.get("items", {}).get("$ref")
            decision_definition = (
                definitions.get(decision_ref.removeprefix("#/$defs/"))
                if isinstance(decision_ref, str)
                else None
            )
            decision_properties = (
                decision_definition.get("properties")
                if isinstance(decision_definition, dict)
                else None
            )
            if not isinstance(decision_properties, dict):
                raise ValueError("Spec payload schema does not expose decision fields")
            decision_properties["id"]["enum"] = [
                str(item.decision_id) for item in bindings
            ]
            decision_properties["status"]["const"] = "confirmed"

        validate_openai_strict_schema(bound)
        return bound

    @staticmethod
    def _candidate_from_provider_output(
        operation: contracts.AnalyzerOperation,
        candidate_type: type[BaseModel],
        output_text: str,
    ) -> BaseModel:
        if operation not in ARTIFACT_PAYLOAD_SCHEMAS:
            return candidate_type.model_validate_json(output_text)
        envelope = json.loads(output_text)
        if not isinstance(envelope, dict):
            raise ValueError("artifact candidate envelope must be an object")
        payload = envelope.get("candidate_payload_json")
        if not isinstance(payload, dict):
            raise ValueError("artifact candidate payload must be an object")
        envelope["candidate_payload_json"] = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        )
        return candidate_type.model_validate_json(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _bind_candidate_echo_schema(
        request: contracts.AnalyzerProviderRequest,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Constrain provider echoes without replacing local admission checks."""

        bound = deepcopy(schema)
        properties = bound.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("candidate schema must expose root properties")
        request_values = request.model_dump(mode="json")
        for field in (
            "analyzer_run_id",
            "context_id",
            "request_hash",
            "source_set_hash",
            "based_on_case_revision",
        ):
            field_schema = properties.get(field)
            if not isinstance(field_schema, dict) or field not in request_values:
                raise ValueError(f"candidate schema cannot bind {field}")
            field_schema["const"] = request_values[field]
        if isinstance(
            request,
            (
                contracts.SpecPackageSynthesisRequest,
                contracts.TechnicalContractSynthesisRequest,
            ),
        ):
            for field, expected in (
                ("foundation_artifact_id", str(request.target.foundation_artifact_id)),
                ("identity_plan_id", str(request.identity_plan.identity_plan_id)),
                ("identity_plan_version", request.identity_plan.identity_plan_version),
                ("semantic_state_hash", request.identity_plan.semantic_state_hash),
            ):
                field_schema = properties.get(field)
                if not isinstance(field_schema, dict):
                    raise ValueError(f"artifact synthesis schema cannot bind {field}")
                field_schema["const"] = expected
        if isinstance(request, contracts.GenerateReviewNarrationRequest):
            for field, expected in (
                ("decision_batch_view_id", str(request.review_view.view_id)),
                ("decision_batch_view_hash", request.review_view.view_hash),
            ):
                field_schema = properties.get(field)
                if not isinstance(field_schema, dict):
                    raise ValueError(f"review narration schema cannot bind {field}")
                field_schema["const"] = expected

            items_schema = properties.get("items")
            if not isinstance(items_schema, dict):
                raise ValueError("review narration schema cannot bind items")
            item_schema = items_schema.get("items")
            reference = item_schema.get("$ref") if isinstance(item_schema, dict) else None
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                raise ValueError("review narration item schema is unresolved")
            definitions = bound.get("$defs")
            definition = (
                definitions.get(reference.removeprefix("#/$defs/"))
                if isinstance(definitions, dict)
                else None
            )
            item_properties = (
                definition.get("properties") if isinstance(definition, dict) else None
            )
            handle_schema = (
                item_properties.get("handle")
                if isinstance(item_properties, dict)
                else None
            )
            if not isinstance(handle_schema, dict):
                raise ValueError("review narration schema cannot bind handles")
            handles = [item.handle for item in request.review_view.items]
            items_schema["minItems"] = len(handles)
            items_schema["maxItems"] = len(handles)
            handle_schema["enum"] = handles
        return bound

    def _emit_lifecycle(
        self,
        *,
        event: str,
        operation: contracts.AnalyzerOperation,
        client_request_id: str,
        started_at: float,
        response: Any | None = None,
        status: str | None = None,
        request_arguments: dict[str, Any] | None = None,
    ) -> None:
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        (
            submitted_max_output_tokens,
            strict_schema_shape_sha256,
            strict_schema_bytes,
        ) = _safe_request_envelope(request_arguments)
        (
            output_item_count,
            output_text_present,
            output_text_character_count,
        ) = _safe_output_shape(response)
        value = ProviderLifecycleEvent(
            event=event,
            operation=operation.value,
            client_request_id=client_request_id,
            response_id=_safe_provider_identifier(getattr(response, "id", None)),
            provider_request_id=_safe_provider_identifier(
                getattr(response, "_request_id", None)
            ),
            status=status or _safe_provider_identifier(getattr(response, "status", None)),
            incomplete_reason=_safe_incomplete_reason(response),
            duration_ms=max(0, int((self._monotonic() - started_at) * 1000)),
            input_tokens=_safe_token_count(getattr(usage, "input_tokens", None)),
            cached_input_tokens=_safe_token_count(
                getattr(input_details, "cached_tokens", None)
            ),
            output_tokens=_safe_token_count(getattr(usage, "output_tokens", None)),
            reasoning_output_tokens=_safe_token_count(
                getattr(output_details, "reasoning_tokens", None)
            ),
            total_tokens=_safe_token_count(getattr(usage, "total_tokens", None)),
            submitted_max_output_tokens=submitted_max_output_tokens,
            response_max_output_tokens=_safe_token_count(
                getattr(response, "max_output_tokens", None)
            ),
            strict_schema_shape_sha256=strict_schema_shape_sha256,
            strict_schema_bytes=strict_schema_bytes,
            output_item_count=output_item_count,
            output_text_present=output_text_present,
            output_text_character_count=output_text_character_count,
        )
        self._lifecycle_events.append(value)
        self._logger.info(
            json.dumps(value.__dict__, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _instructions(operation: contracts.AnalyzerOperation) -> str:
        instructions = (
            "You are the SpecOps Workshop Analyzer. Use the two source documents already "
            "attached to this Conversation and the new Foundation-bound request. Return only "
            f"the strict {operation.value} candidate. Propose semantics; never claim authority, "
            "confirmation, readiness, Foundation commands, or new canonical identities. "
            "For every QUOTE_SEARCH evidence candidate, copy one exact source substring into "
            "both locator.exact_quote and quoted_text_candidate; those two strings must be "
            "character-for-character identical. Silently verify that exact_quote occurs "
            "verbatim at the requested occurrence in the named attached source. If it does "
            "not, omit that evidence candidate and every proposal that depends on it; never "
            "reconstruct, normalize, or approximately quote source text."
        )
        if operation is contracts.AnalyzerOperation.BOOTSTRAP:
            instructions += (
                " Build initial_runway from exactly four distinct questions: one "
                "recommended_question_key and three safe_alternate_question_keys. Every "
                "selected question must set safe_without_current_turn_interpretation=true "
                "and must have an empty prerequisite_problem_keys array, because every "
                "BOOTSTRAP problem is initially OPEN. Never select a prerequisite-bound or "
                "unsafe question into initial_runway; place it in do_not_ask_question_keys "
                "when it must be explicitly excluded."
            )
        if operation is contracts.AnalyzerOperation.TURN_ANALYSIS:
            instructions += (
                " Treat an explicit decision replacement in the finalized transcript as a "
                "decision candidate, not only as a problem or follow-up question. When the "
                "speaker explicitly rejects, changes, or replaces a reviewed decision and "
                "states its replacement, include exactly the stated replacement in decisions "
                "with requires_human_confirmation=true. Bind existing_decision_ref to the exact "
                "prior Foundation decision when the transcript and snapshot make that relation "
                "unambiguous; otherwise leave the ref null. Do not invent a replacement when "
                "the speaker did not state one or the intended replacement is ambiguous. For "
                "every decisions[].problem_links[].problem_ref that uses CANDIDATE_KEY, reference "
                "only a candidate_key declared in new_problems; never reference an "
                "evidence_candidates key there. Put supporting evidence only in "
                "decisions[].evidence_refs, where each CANDIDATE_KEY must reference a key "
                "declared in evidence_candidates."
            )
        if operation is contracts.AnalyzerOperation.GUIDANCE:
            instructions += (
                " Select recommended_question and safe_alternates only from Foundation "
                "question records whose safe_without_current_turn_interpretation field is "
                "true, whose addresses_problem_refs identify current OPEN problems, whose "
                "prerequisite_problem_refs identify RESOLVED problems, and that are neither "
                "already ASKED nor listed in "
                "do_not_ask_question_refs. Put an unsafe, dependency-ineligible, or already-asked "
                "question in neither selected field; Foundation will reject that selected branch "
                "rather than reinterpret or repeat it. Recommend the eligible question addressing "
                "the highest-severity open problem first, ordered CRITICAL, HIGH, MEDIUM, LOW. "
                "Use each question identity at most once "
                "across recommended_question and safe_alternates: never repeat the recommended "
                "question as an alternate and never duplicate an alternate. Also use each "
                "do_not_ask_question_refs identity at most once, and never place any selected "
                "question identity in do_not_ask_question_refs."
            )
        if operation in ARTIFACT_PAYLOAD_SCHEMAS:
            instructions += (
                " Populate candidate_payload_json as exactly one nested JSON object that "
                "validates against the provider-owned payload schema supplied in this request; "
                "never use Markdown, prose, or a JSON-encoded string in that field. "
            )
            if operation is contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS:
                instructions += (
                    "Every identity-bearing provider collection is a fixed object whose property "
                    "names are typed local slot_key handles. A property value is either one semantic "
                    "item or null when that capacity is unused; never emit an id field inside a value. "
                    "Use only those local handles for references to new items. In "
                    "evidence_support_proposals.claim_pointer, use the local handle property segment "
                    "rather than a numeric array index and terminate at one exact nonempty text "
                    "field allowed by the bound schema; never point at the identity-bearing object. "
                    "Foundation alone assigns canonical UUIDs, "
                    "rehydrates arrays in slot ordinal order, rewrites local references, and validates "
                    "the final graph. Never copy or infer a foundation_id for a NEW_ENTITY slot."
                )
            else:
                instructions += (
                    "Use only identity_plan.planned_identities foundation_id values at payload paths "
                    "for their matching entity_kind, use each selected identity at most once, and "
                    "create no other payload-owned identity; unused capacity identities are allowed. "
                    "Foundation will independently enforce the payload schema and identity plan."
                )
        if operation is contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS:
            instructions += (
                " Treat construction_blueprint and quality_rule_manifest as the exact construction "
                "checklist for this draft. Populate only ANALYZER-owned construction slots. Do not "
                "emit actors or decisions; Foundation deterministically projects those server-owned "
                "records after provider completion. Do not emit evidence_catalog or "
                "semantic_evidence_findings. Instead, propose exact claim/evidence pairs only in "
                "evidence_support_proposals; use ANALYZER-owned claim pointers plus Foundation-owned "
                "EVIDENCE and SEMANTIC_EVIDENCE_FINDING identities, and copy an exact source excerpt. "
                "Do not emit evidence_refs or source_evidence_refs; those fields are absent from the "
                "provider-owned schema and Foundation constructs them only after exact-quote validation. "
                "Foundation computes every hash before a separate SUPPORTS assessment. Use "
                "foundation_owned_record_refs only when other "
                "artifact items need to reference those canonical identities. Construct the draft "
                "without claiming that any rule passed. Use enough separately identified, atomic "
                "requirements, behavior rules, data rules, scenarios, experience states, and "
                "acceptance checks to satisfy its stated coverage conditions; do not collapse "
                "independently testable obligations into the schema-minimum single item. All payload "
                "decision_refs must reference the matching Foundation-owned decision identity, never "
                "a copied identity. Reference those "
                "decisions from every requirement or rule they govern. Do not invent, defer, or "
                "replace a Foundation-confirmed decision, and do not introduce a new deferred "
                "decision unless the current Foundation snapshot contains a genuinely unresolved "
                "problem. Evidence excerpts must support the exact atomic claim that cites them; a "
                "source question alone never supports a broader combined requirement."
            )
        elif operation is contracts.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS:
            instructions += (
                " Treat construction_blueprint and technical_closure_manifest as the exact, "
                "Foundation-owned Technical construction checklist. Cover every manifest "
                "obligation through an implements_spec_refs, covers_acceptance_refs, or "
                "spec_quality_ref edge; never omit an obligation merely because the payload "
                "schema permits a smaller collection. Represent each component and substrate "
                "dependency exactly once in architecture_context.nodes by technical_ref. "
                "Use one interface record per independently executable operation and complete "
                "all input, output, authorization, failure, idempotency, timeout, and versioning "
                "semantics. Define initial_state for every workflow and owner for the rollout. "
                "Engineering decisions contain only concrete evidence-backed choices. Put an "
                "unresolved choice in review_obligations instead; do not invent a resolution. "
                "Construction slots are bounded capacity and may remain unused, but every emitted "
                "identity must retain the planned kind and Foundation identity."
            )
        return instructions

    @staticmethod
    def _validate_context(
        request: contracts.AnalyzerProviderRequest,
        context: contracts.AnalyzerContextBinding,
    ) -> None:
        if context.status is not contracts.ContextStatus.ACTIVE:
            raise ValueError("Analyzer context is not active")
        if request.context_id != context.context_id:
            raise ValueError("request context ID does not match the active context")
        if request.provider_conversation_id != context.provider_conversation_id:
            raise ValueError("request Conversation does not match the active context")
        if request.source_set_hash != context.source_set.source_set_hash:
            raise ValueError("request source set does not match the active context")
        if request.analyzer_contract != context.analyzer_contract:
            raise ValueError("request Analyzer contract does not match the active context")
        if context.model != "gpt-5.6-terra" or context.reasoning_effort != "medium":
            raise ValueError("active context violates the fixed provider profile")
        if not context.conversation_state_persisted or not context.response_store_enabled:
            raise ValueError("active context does not prove stored Conversation policy")

    @staticmethod
    def _validate_candidate_echo(request: BaseModel, candidate: BaseModel) -> None:
        for field in ("analyzer_run_id", "context_id", "request_hash", "source_set_hash"):
            if getattr(candidate, field) != getattr(request, field):
                raise ValueError(f"provider candidate does not echo {field}")
        if hasattr(candidate, "based_on_case_revision") and (
            candidate.based_on_case_revision != request.based_on_case_revision
        ):
            raise ValueError("provider candidate does not echo based_on_case_revision")
        if isinstance(
            request,
            (
                contracts.SpecPackageSynthesisRequest,
                contracts.TechnicalContractSynthesisRequest,
            ),
        ):
            expected = {
                "foundation_artifact_id": request.target.foundation_artifact_id,
                "identity_plan_id": request.identity_plan.identity_plan_id,
                "identity_plan_version": request.identity_plan.identity_plan_version,
                "semantic_state_hash": request.identity_plan.semantic_state_hash,
            }
            for field, value in expected.items():
                if getattr(candidate, field) != value:
                    raise ValueError(f"provider candidate does not echo {field}")

    @staticmethod
    def source_set_hash(sources: tuple[contracts.SourceIdentity, contracts.SourceIdentity]) -> str:
        # The production canonical helper replaces this local import; keeping the
        # domain-separated recipe here avoids provider IDs entering the binding.
        from .canonical import domain_hash

        material = [
            {
                "source_id": str(item.source_id),
                "role": item.role.value,
                "version": item.version,
                "payload_hash": item.payload_hash,
            }
            for item in sources
        ]
        return domain_hash("SPECOPS:SOURCE_SET:v1", material)

    def _output_error(
        self,
        client_request_id: str,
        error: Exception,
        *,
        provider_request_id: str | None = None,
    ) -> ProviderAdapterError:
        receipt = contracts.ProviderFailureReceipt(
            provider=contracts.ProviderName.OPENAI,
            stage=contracts.ProviderProcessingStage.LOCAL_VALIDATION,
            client_request_id=client_request_id,
            provider_request_id=provider_request_id,
            status_code=None,
            code=contracts.ProviderFailureCode.OUTPUT_INVALID,
            retryable=False,
            validation_diagnostics=safe_validation_diagnostics(error),
            occurred_at=self._now(),
        )
        self._failures.append(receipt)
        correction_code = None
        if isinstance(error, ValidationError):
            known = {
                "question references unknown problem": (
                    "UNKNOWN_QUESTION_PROBLEM_REFERENCE"
                ),
            }
            messages = {
                str(item.get("ctx", {}).get("error", ""))
                for item in error.errors(
                    include_url=False,
                    include_context=True,
                    include_input=False,
                )
            }
            correction_code = next(
                (known[message] for message in messages if message in known),
                None,
            )
        return ProviderAdapterError(
            receipt, bootstrap_correction_code=correction_code
        )

    def _provider_error(
        self,
        error: Exception,
        *,
        stage: contracts.ProviderProcessingStage,
        client_request_id: str,
    ) -> ProviderAdapterError:
        status = getattr(error, "status_code", None)
        status_code = status if isinstance(status, int) and 100 <= status <= 599 else None
        code_value = getattr(error, "code", None)
        parameter = getattr(error, "param", None)
        code, retryable = self._classify(
            status_code,
            code_value,
            parameter,
            stage,
            type(error).__name__,
        )
        receipt = contracts.ProviderFailureReceipt(
            provider=contracts.ProviderName.OPENAI,
            stage=stage,
            client_request_id=client_request_id,
            provider_request_id=_safe_provider_identifier(getattr(error, "request_id", None)),
            status_code=status_code,
            code=code,
            retryable=retryable,
            validation_diagnostics=(
                safe_provider_schema_diagnostics(error)
                if code
                in {
                    contracts.ProviderFailureCode.SCHEMA_REJECTED,
                    contracts.ProviderFailureCode.UNSUPPORTED_SCHEMA_KEYWORD,
                }
                else ()
            ),
            occurred_at=self._now(),
        )
        self._failures.append(receipt)
        return ProviderAdapterError(receipt)

    @staticmethod
    def _classify(status_code, code, parameter, stage, error_type):
        if isinstance(code, str):
            code = code.lower()
        if isinstance(parameter, str):
            parameter = parameter.lower()
        if status_code == 400:
            if parameter and ("text.format" in parameter or "schema" in parameter):
                if code in {"unsupported_schema_keyword", "unsupported_value"}:
                    return contracts.ProviderFailureCode.UNSUPPORTED_SCHEMA_KEYWORD, False
                return contracts.ProviderFailureCode.SCHEMA_REJECTED, False
            if parameter and "conversation" in parameter:
                return contracts.ProviderFailureCode.CONVERSATION_UNAVAILABLE, True
            if parameter and ("file" in parameter or "input" in parameter) and code in {
                "invalid_file",
                "file_not_found",
                "unsupported_file",
            }:
                return contracts.ProviderFailureCode.INPUT_FILE_UNAVAILABLE, True
            if code in {"context_mismatch", "conversation_context_mismatch"}:
                return contracts.ProviderFailureCode.CONTEXT_MISMATCH, True
            if code in {"context_length_exceeded", "request_too_large"}:
                return contracts.ProviderFailureCode.REQUEST_TOO_LARGE, False
            if code in {"invalid_request_error", "invalid_parameter"}:
                return contracts.ProviderFailureCode.INVALID_REQUEST_SHAPE, False
            return contracts.ProviderFailureCode.UNKNOWN_SAFE, False
        exact = {
            401: contracts.ProviderFailureCode.HTTP_401,
            403: contracts.ProviderFailureCode.HTTP_403,
            404: contracts.ProviderFailureCode.HTTP_404,
            409: contracts.ProviderFailureCode.HTTP_409,
            429: contracts.ProviderFailureCode.HTTP_429,
        }
        if status_code in exact:
            return exact[status_code], status_code in {409, 429}
        if status_code is not None and 500 <= status_code <= 599:
            return contracts.ProviderFailureCode.HTTP_5XX, True
        normalized_error_type = error_type.lower() if isinstance(error_type, str) else ""
        if status_code is None and "timeout" in normalized_error_type:
            return contracts.ProviderFailureCode.TIMEOUT, True
        if status_code is None and "connection" in normalized_error_type:
            return contracts.ProviderFailureCode.CONNECTION, True
        if stage is contracts.ProviderProcessingStage.CONVERSATION_CREATE and status_code == 404:
            return contracts.ProviderFailureCode.CONVERSATION_UNAVAILABLE, True
        return contracts.ProviderFailureCode.UNKNOWN_SAFE, False
