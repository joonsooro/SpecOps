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
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from . import contracts
from .schema_compiler import native_schema_for


PROVIDER_IO_TIMEOUT_SECONDS = 60.0
SLOW_RESPONSE_OBSERVATION_SECONDS = 60.0
BACKGROUND_POLL_SECONDS = 1.0
MAX_OUTPUT_TOKENS = {
    contracts.AnalyzerOperation.BOOTSTRAP: 24_000,
    contracts.AnalyzerOperation.TURN_ANALYSIS: 20_000,
    contracts.AnalyzerOperation.GUIDANCE: 8_000,
    contracts.AnalyzerOperation.REVIEW_NARRATION: 8_000,
    contracts.AnalyzerOperation.SPEC_PACKAGE_SYNTHESIS: 24_000,
    contracts.AnalyzerOperation.TECHNICAL_CONTRACT_SYNTHESIS: 24_000,
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
    duration_ms: int
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None


class ProviderAdapterError(RuntimeError):
    """A provider failure whose only public detail is a safe receipt."""

    def __init__(self, receipt: contracts.ProviderFailureReceipt) -> None:
        super().__init__(receipt.code.value)
        self.receipt = receipt


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


def safe_validation_diagnostics(error: Exception) -> tuple[contracts.SafeValidationDiagnostic, ...]:
    if not isinstance(error, ValidationError):
        return ()
    result: list[contracts.SafeValidationDiagnostic] = []
    for item in error.errors(include_url=False, include_context=False, include_input=False):
        path: list[str] = []
        for segment in item.get("loc", ())[:16]:
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
        self._background_responses: dict[str, Any] = {}
        self._background_started_at: dict[str, float] = {}

    @property
    def failure_receipts(self) -> tuple[contracts.ProviderFailureReceipt, ...]:
        return tuple(self._failures)

    @property
    def lifecycle_events(self) -> tuple[ProviderLifecycleEvent, ...]:
        return tuple(self._lifecycle_events)

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
        self, conversation_id: str | None, file_ids: tuple[str, ...]
    ) -> None:
        await self._release_prepared_ids(conversation_id, file_ids)

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

        if request.provider_conversation_id != prepared.provider_conversation_id:
            raise ValueError("bootstrap request does not bind the prepared Conversation")
        if request.source_set != prepared.source_set:
            raise ValueError("bootstrap request does not bind the two uploaded sources")
        operation = contracts.AnalyzerOperation.BOOTSTRAP
        started_at = self._monotonic()
        self._emit_lifecycle(
            event="provider_request.started",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
        )
        arguments = self._response_arguments(request, bootstrap=True)
        arguments["background"] = True
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
            )
            raise
        except TimeoutError as exc:
            self._emit_lifecycle(
                event="provider_request.timeout",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                status="CREATE_ID_UNCERTAIN",
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

    async def release_context(self, context: contracts.AnalyzerContextBinding) -> None:
        """Best-effort provider cleanup after Foundation has invalidated a binding.

        Foundation invalidation is authoritative and happens first. Cleanup is
        bounded and idempotent; no source content or provider error text is
        retained when a remote object has already disappeared.
        """

        await self._release_prepared_ids(
            context.provider_conversation_id,
            tuple(item.provider_file_id for item in context.source_set.ordered_sources),
        )

    async def release_prepared(self, prepared: PreparedProviderContext) -> None:
        await self._release_prepared_ids(
            prepared.provider_conversation_id,
            tuple(item.provider_file_id for item in prepared.source_set.ordered_sources),
        )

    async def _release_prepared_ids(
        self, conversation_id: str | None, file_ids: tuple[str, ...]
    ) -> None:
        async def bounded(resource: Any, identifier: str, request_id: str) -> None:
            delete = getattr(resource, "delete", None)
            if delete is None:
                return
            try:
                await asyncio.wait_for(
                    delete(
                        identifier,
                        extra_headers={"X-Client-Request-Id": request_id},
                    ),
                    timeout=PROVIDER_IO_TIMEOUT_SECONDS,
                )
            except Exception:
                return

        if conversation_id is not None:
            await bounded(
                self._client.conversations,
                conversation_id,
                f"specops-conversation-delete-{conversation_id}",
            )
        for file_id in file_ids:
            await bounded(
                self._client.files,
                file_id,
                f"specops-file-delete-{file_id}",
            )

    async def _execute(self, request: contracts.AnalyzerProviderRequest, *, bootstrap: bool):
        operation = contracts.AnalyzerOperation(request.request_type)
        _, _, candidate_type = native_schema_for(operation)
        arguments = self._response_arguments(request, bootstrap=bootstrap)
        started_at = self._monotonic()
        self._emit_lifecycle(
            event="provider_request.started",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
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
            )
            raise
        except TimeoutError as exc:
            self._emit_lifecycle(
                event="provider_request.timeout",
                operation=operation,
                client_request_id=request.client_request_id,
                started_at=started_at,
                status="CREATE_ID_UNCERTAIN",
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

        self._emit_lifecycle(
            event="provider_request.completed",
            operation=operation,
            client_request_id=request.client_request_id,
            started_at=started_at,
            response=response,
        )

        response_id = _safe_provider_identifier(getattr(response, "id", None))
        if response_id is None:
            raise self._output_error(request.client_request_id, ValueError("unsafe response ID"))
        try:
            candidate = candidate_type.model_validate_json(response.output_text)
            self._validate_candidate_echo(request, candidate)
        except Exception as exc:
            raise self._output_error(
                request.client_request_id,
                exc,
                provider_request_id=_safe_provider_identifier(getattr(response, "_request_id", None)),
            ) from exc
        return candidate, response_id

    def _response_arguments(
        self, request: contracts.AnalyzerProviderRequest, *, bootstrap: bool
    ) -> dict[str, Any]:
        operation = contracts.AnalyzerOperation(request.request_type)
        schema_name, schema, _ = native_schema_for(operation)
        content: list[dict[str, str]] = []
        if bootstrap:
            assert isinstance(request, contracts.BootstrapAnalyzerRequest)
            content.extend(
                {"type": "input_file", "file_id": item.provider_file_id}
                for item in request.source_set.ordered_sources
            )
        content.append(
            {
                "type": "input_text",
                "text": request.model_dump_json(exclude_none=False),
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

    def _emit_lifecycle(
        self,
        *,
        event: str,
        operation: contracts.AnalyzerOperation,
        client_request_id: str,
        started_at: float,
        response: Any | None = None,
        status: str | None = None,
    ) -> None:
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        value = ProviderLifecycleEvent(
            event=event,
            operation=operation.value,
            client_request_id=client_request_id,
            response_id=_safe_provider_identifier(getattr(response, "id", None)),
            provider_request_id=_safe_provider_identifier(
                getattr(response, "_request_id", None)
            ),
            status=status or _safe_provider_identifier(getattr(response, "status", None)),
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
        )
        self._lifecycle_events.append(value)
        self._logger.info(
            json.dumps(value.__dict__, sort_keys=True, separators=(",", ":"))
        )

    @staticmethod
    def _instructions(operation: contracts.AnalyzerOperation) -> str:
        return (
            "You are the SpecOps Workshop Analyzer. Use the two source documents already "
            "attached to this Conversation and the new Foundation-bound request. Return only "
            f"the strict {operation.value} candidate. Propose semantics; never claim authority, "
            "confirmation, readiness, Foundation commands, or new canonical identities."
        )

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
        return ProviderAdapterError(receipt)

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
            validation_diagnostics=(),
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
