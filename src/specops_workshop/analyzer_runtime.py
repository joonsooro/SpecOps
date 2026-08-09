from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from specops_workflow.canonical import sha256 as canonical_sha256

from .evidence import EvidenceIndex, RETRIEVAL_POLICY_VERSION


ANALYZER_PRIMARY_SECONDS = 10.0
ANALYZER_PROVIDER_CUTOFF_SECONDS = 25.0
ANALYZER_HARD_DEADLINE_SECONDS = 30.0
ANALYZER_LOCAL_BUDGET_MS = 250
ANALYZER_RETRY_DELAY_SECONDS = 0.25


class AnalyzerDeadlineExceeded(TimeoutError):
    pass


ProviderValue = TypeVar("ProviderValue")


@dataclass(frozen=True)
class AnalyzerSessionDeadline:
    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    started_at: float

    @classmethod
    def start(
        cls,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> AnalyzerSessionDeadline:
        return cls(monotonic=monotonic, sleep=sleep, started_at=monotonic())

    @property
    def elapsed_seconds(self) -> float:
        elapsed = self.monotonic() - self.started_at
        if elapsed < 0:
            raise ValueError("analyzer monotonic clock moved backwards")
        return elapsed

    @property
    def remaining_hard_seconds(self) -> float:
        return max(0.0, ANALYZER_HARD_DEADLINE_SECONDS - self.elapsed_seconds)

    @property
    def remaining_usable_ms(self) -> int:
        remaining = ANALYZER_PROVIDER_CUTOFF_SECONDS - self.elapsed_seconds
        return max(1, min(25_000, int(remaining * 1000)))

    @property
    def remaining_provider_seconds(self) -> float:
        return max(
            0.0,
            min(
                ANALYZER_PROVIDER_CUTOFF_SECONDS - self.elapsed_seconds,
                self.remaining_hard_seconds,
            ),
        )

    def ensure_hard_budget(self) -> None:
        if self.elapsed_seconds >= ANALYZER_HARD_DEADLINE_SECONDS:
            raise AnalyzerDeadlineExceeded("analyzer session hard deadline exhausted")

    def can_start_provider_call(self) -> bool:
        return self.elapsed_seconds < ANALYZER_PROVIDER_CUTOFF_SECONDS

    def within_usable_window(self) -> bool:
        return self.elapsed_seconds <= ANALYZER_PROVIDER_CUTOFF_SECONDS

    async def wait_for_retry(self) -> bool:
        if (
            self.elapsed_seconds + ANALYZER_RETRY_DELAY_SECONDS
            >= ANALYZER_PROVIDER_CUTOFF_SECONDS
        ):
            return False
        await self.sleep(ANALYZER_RETRY_DELAY_SECONDS)
        return self.can_start_provider_call()

    async def run_provider(
        self, operation: Awaitable[ProviderValue]
    ) -> ProviderValue:
        self.ensure_hard_budget()
        if not self.can_start_provider_call():
            if hasattr(operation, "close"):
                operation.close()  # type: ignore[attr-defined]
            raise AnalyzerDeadlineExceeded("provider cutoff exhausted")
        try:
            return await asyncio.wait_for(
                operation,
                timeout=self.remaining_provider_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise AnalyzerDeadlineExceeded(
                "analyzer session hard deadline exhausted"
            ) from exc


def source_snapshot_fingerprint(index: EvidenceIndex) -> str:
    return canonical_sha256({
        "schema": "analyzer-source-snapshot-v1",
        "case_id": index.case_id,
        "seals": [
            {
                "source_role": value.source_role,
                "artifact_id": value.artifact_id,
                "version": value.version,
                "content_hash": value.content_hash,
                "media_type": value.media_type,
                "line_count": value.line_count,
            }
            for value in index.source_seals
        ],
    })


def retrieval_checkpoint_fingerprint(
    *,
    source_fingerprint: str,
    final_turn_fingerprint: str,
    selected_aliases: tuple[str, ...],
) -> str:
    return canonical_sha256({
        "schema": "analyzer-retrieval-checkpoint-v1",
        "retrieval_policy": RETRIEVAL_POLICY_VERSION,
        "source_fingerprint": source_fingerprint,
        "final_turn_fingerprint": final_turn_fingerprint,
        "selected_aliases": selected_aliases,
    })


def analyzer_request_fingerprint(
    *,
    source_fingerprint: str,
    final_turn_fingerprint: str,
    committed_semantics_fingerprint: str,
    edit_instruction_fingerprint: str,
    purpose: str,
    phase: str,
) -> str:
    return canonical_sha256({
        "schema": "semantic-analyzer-request-v1",
        "retrieval_policy": RETRIEVAL_POLICY_VERSION,
        "source_fingerprint": source_fingerprint,
        "final_turn_fingerprint": final_turn_fingerprint,
        "committed_semantics_fingerprint": committed_semantics_fingerprint,
        "edit_instruction_fingerprint": edit_instruction_fingerprint,
        "purpose": purpose,
        "phase": phase,
    })
