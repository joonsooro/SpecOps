from __future__ import annotations

import json
import logging
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid5

from pydantic import Field

from .contracts import WorkshopModel


class TelemetryStage(StrEnum):
    BROWSER = "BROWSER"
    GATEWAY = "GATEWAY"
    ANALYZER = "ANALYZER"
    FOUNDATION = "FOUNDATION"
    PLAYBACK = "PLAYBACK"


class SpanOutcome(StrEnum):
    OK = "OK"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class LatencyMetric(StrEnum):
    PARTIAL_DISPLAY = "PARTIAL_DISPLAY"
    FINAL_DISPLAY = "FINAL_DISPLAY"
    FIRST_AGENT_AUDIO = "FIRST_AGENT_AUDIO"
    BARGE_IN_STOP = "BARGE_IN_STOP"


class LatencySpan(WorkshopModel):
    span_id: UUID
    session_id: UUID
    stage: TelemetryStage
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    outcome: SpanOutcome | None = None


class BrowserSpanInput(WorkshopModel):
    span_id: UUID
    stage: TelemetryStage
    duration_ms: int = Field(ge=0, le=60_000)
    outcome: SpanOutcome

    def model_post_init(self, _context: Any) -> None:
        if self.stage not in {TelemetryStage.BROWSER, TelemetryStage.PLAYBACK}:
            raise ValueError("the browser may record only browser or playback spans")


class LatencyMeasurement(WorkshopModel):
    turn_sequence: int = Field(ge=1)
    metric: LatencyMetric
    duration_ms: int = Field(ge=0, le=60_000)


def release_latency_p95(
    measurements: tuple[LatencyMeasurement, ...],
) -> dict[LatencyMetric, int]:
    result: dict[LatencyMetric, int] = {}
    for metric in LatencyMetric:
        values = sorted(
            value.duration_ms for value in measurements if value.metric == metric
        )
        if len(values) < 30:
            raise ValueError(f"{metric.value} requires at least 30 measured turns")
        result[metric] = values[(95 * len(values) + 99) // 100 - 1]
    if not 150 <= result[LatencyMetric.PARTIAL_DISPLAY] <= 500:
        raise ValueError("partial-display p95 is outside 150-500 ms")
    if not 300 <= result[LatencyMetric.FINAL_DISPLAY] <= 1000:
        raise ValueError("final-display p95 is outside 300-1000 ms")
    if result[LatencyMetric.FIRST_AGENT_AUDIO] > 3000:
        raise ValueError("first-agent-audio p95 exceeds 3000 ms")
    if result[LatencyMetric.BARGE_IN_STOP] > 300:
        raise ValueError("barge-in p95 exceeds 300 ms")
    return result


class OperationalEvent(WorkshopModel):
    event: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    stage: TelemetryStage
    session_id: UUID
    span_id: UUID
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    outcome: SpanOutcome
    error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


_REDACTED_KEYS = frozenset({
    "api_key",
    "authorization",
    "credential",
    "credentials",
    "prompt",
    "raw_audio",
    "source_content",
    "source_text",
    "token",
    "transcript",
    "transcript_text",
})


def redact_operational_fields(value: Any) -> Any:
    """Defense-in-depth redaction for data before it reaches a log sink."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).lower() in _REDACTED_KEYS
            else redact_operational_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_operational_fields(item) for item in value]
    if isinstance(value, bytes):
        return "[REDACTED_BYTES]"
    return value


class JsonTelemetry:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("specops.workshop")

    def emit(self, value: OperationalEvent) -> None:
        payload = redact_operational_fields(value.model_dump(mode="json"))
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


TELEMETRY_NAMESPACE = UUID("32ea9f44-68d7-5bc8-9a85-727329ee0af1")


class TelemetryRecorder:
    def __init__(self, store, *, clock, sink: JsonTelemetry | None = None) -> None:
        self.store = store
        self.clock = clock
        self.sink = sink or JsonTelemetry()

    def start(
        self,
        *,
        session_id: UUID,
        stage: TelemetryStage,
        operation_id: str,
    ) -> UUID:
        span_id = uuid5(TELEMETRY_NAMESPACE, f"{session_id}:{stage.value}:{operation_id}")
        self.store.start_latency_span(LatencySpan(
            span_id=span_id,
            session_id=session_id,
            stage=stage,
            started_at=self.clock.now(),
        ))
        return span_id

    def finish(
        self,
        span_id: UUID,
        *,
        outcome: SpanOutcome,
        error_code: str | None = None,
    ) -> LatencySpan:
        span = self.store.finish_latency_span(
            span_id,
            ended_at=self.clock.now(),
            outcome=outcome,
        )
        self.sink.emit(OperationalEvent(
            event="latency_span.completed",
            stage=span.stage,
            session_id=span.session_id,
            span_id=span.span_id,
            duration_ms=span.duration_ms,
            outcome=span.outcome or outcome,
            error_code=error_code,
        ))
        return span

    def record_browser(self, session_id: UUID, value: BrowserSpanInput) -> LatencySpan:
        ended_at = self.clock.now()
        from datetime import timedelta

        span = LatencySpan(
            span_id=value.span_id,
            session_id=session_id,
            stage=value.stage,
            started_at=ended_at - timedelta(milliseconds=value.duration_ms),
            ended_at=ended_at,
            duration_ms=value.duration_ms,
            outcome=value.outcome,
        )
        stored = self.store.record_completed_latency_span(span)
        self.sink.emit(OperationalEvent(
            event="latency_span.completed",
            stage=stored.stage,
            session_id=stored.session_id,
            span_id=stored.span_id,
            duration_ms=stored.duration_ms,
            outcome=stored.outcome or value.outcome,
        ))
        return stored
