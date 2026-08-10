from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from .analyzer import AnalyzerTurnResult


@dataclass(frozen=True)
class FinalTurnProcessed:
    committed: object
    analyzer_result: AnalyzerTurnResult | None


class FinalTurnProcessor:
    """The single post-finalization seam shared by HTTP and voice transports."""

    def __init__(self, coordinator, gate) -> None:
        self.coordinator = coordinator
        self.gate = gate

    async def process(
        self,
        session_id: UUID,
        *,
        turn_sequence: int,
        text: str,
        provider_request_id: str,
        correction_of_version: int | None = None,
    ) -> FinalTurnProcessed:
        committed = self.coordinator.commit_final_turn(
            session_id,
            turn_sequence=turn_sequence,
            text=text,
            provider_request_id=provider_request_id,
            correction_of_version=correction_of_version,
        )
        analyzer_result = (
            None
            if self.gate is None
            else await self.gate.analyze_final_turn(session_id, turn_sequence)
        )
        return FinalTurnProcessed(
            committed=committed,
            analyzer_result=analyzer_result,
        )
