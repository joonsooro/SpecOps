from __future__ import annotations

from openai import AsyncOpenAI

from ..analyzer import AnalyzerRequest, AnalyzerTurnResult, SpecAnalyzerProvider
from ..config import TERRA_MODEL


class TerraResponsesProvider(SpecAnalyzerProvider):
    def __init__(self, *, api_key: str, model: str = TERRA_MODEL, client=None) -> None:
        if model != TERRA_MODEL:
            raise ValueError("Terra model must match the pinned Workshop model")
        self._client = client or AsyncOpenAI(api_key=api_key)
        self._model = model

    async def analyze(self, request: AnalyzerRequest) -> AnalyzerTurnResult:
        response = await self._client.responses.create(
            model=self._model,
            reasoning={"effort": request.effort},
            store=False,
            input=[{
                "role": "user",
                "content": (
                    "Analyze this provider-final PM Workshop turn. Return only the strict analyzer schema. "
                    "Never invent evidence or platform actions.\n\n"
                    f"Phase: {request.phase.value}\nCommitted context: {request.committed_context_json}\n"
                    f"Final turn: {request.final_turn.normalized_text}"
                ),
            }],
            text={"format": {
                "type": "json_schema", "name": "analyzer_turn_result_v1", "strict": True,
                "schema": AnalyzerTurnResult.model_json_schema(mode="validation"),
            }},
        )
        return AnalyzerTurnResult.model_validate_json(response.output_text)
