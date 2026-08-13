"""Low-latency Gemini transport whose committed inputs use the V4 seam."""

from __future__ import annotations

import asyncio
import json

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter
from specops_contracts import workshop_v1 as c

from specops_workshop.contracts import ConversationPhase, ProviderResumeContext
from specops_workshop.ports import LiveVoiceProvider, VoiceContext, VoiceEventType

from .orchestrator import FinalTranscriptInput, V4ProductionOrchestrator


class V4LiveTransport:
    def __init__(self, provider: LiveVoiceProvider, orchestrator: V4ProductionOrchestrator) -> None:
        self.provider = provider
        self.orchestrator = orchestrator

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_json({"type": "CALL_STATE", "state": "CONNECTING"})
        try:
            session = await self.provider.connect(
                VoiceContext(
                    system_instruction=(
                        "Facilitate the Workshop without interpreting confirmations. "
                        "Return final transcripts and mechanical spoken selections only."
                    ),
                    resume=ProviderResumeContext(
                        conversation_phase=ConversationPhase.WORKSHOP,
                        committed_package=None,
                        downstream_handoff=None,
                        final_transcript_snapshots=(),
                    ),
                )
            )
        except Exception:
            await websocket.send_json(
                {
                    "type": "CALL_STATE",
                    "state": "DISCONNECTED",
                    "guidance": "Voice unavailable. Continue with text.",
                }
            )
            await self._text_only(websocket)
            return
        await websocket.send_json({"type": "CALL_STATE", "state": "LISTENING"})
        client = asyncio.create_task(self._client(websocket, session))
        provider = asyncio.create_task(self._provider(websocket, session))
        try:
            done, pending = await asyncio.wait(
                (client, provider), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        finally:
            await session.close()

    async def _commit(self, websocket: WebSocket, value: dict, *, provider_id: str) -> None:
        receipt = await self.orchestrator.record_final_transcript(
            FinalTranscriptInput(
                turn_sequence=int(value["turn_sequence"]),
                text=str(value["text"]),
                provider_request_id=provider_id,
                speaker_actor_id=self.orchestrator.foundation.case_actor(
                    self.orchestrator.case_id, "PM"
                ),
                actor="PM",
            )
        )
        await websocket.send_json(
            {
                "type": "FINAL_COMMITTED",
                "revision": receipt.transcript.command.resulting_case_revision,
                "duplicate": receipt.duplicate,
            }
        )

    async def _client(self, websocket: WebSocket, session) -> None:
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                await session.send_audio(message["bytes"])
                continue
            if message.get("text") is None:
                return
            value = json.loads(message["text"])
            if value.get("type") == "TEXT":
                await self._commit(
                    websocket,
                    value,
                    provider_id=str(value["provider_request_id"]),
                )
            elif value.get("type") == "INTERRUPT":
                await session.interrupt()
            elif value.get("type") == "DECISION_SELECTION":
                selection = TypeAdapter(c.VoiceConfirmationSelectionCandidate).validate_json(
                    json.dumps(value["selection"]), strict=True
                )
                authentication = TypeAdapter(c.ActorAuthentication).validate_json(
                    json.dumps(value["actor_authentication"]), strict=True
                )
                receipt = self.orchestrator.apply_voice_selection(
                    selection, authentication
                )
                await websocket.send_json(
                    {
                        "type": "DECISION_SELECTION_COMMITTED",
                        "receipt": receipt.model_dump(mode="json"),
                    }
                )
            elif value.get("type") == "END":
                await websocket.send_json({"type": "CALL_STATE", "state": "ENDED"})
                return
            else:
                await websocket.send_json({"type": "ERROR", "code": "INVALID_CONTROL"})

    async def _provider(self, websocket: WebSocket, session) -> None:
        sequence = (
            1
            if self.orchestrator.foundation.latest_final_transcript(
                self.orchestrator.case_id
            )
            is None
            else self.orchestrator.foundation.latest_final_transcript(
                self.orchestrator.case_id
            ).sequence_number
            + 1
        )
        async for event in session.events():
            if event.type is VoiceEventType.AUDIO and event.audio is not None:
                await websocket.send_bytes(event.audio)
            elif event.type is VoiceEventType.INPUT_PARTIAL:
                await websocket.send_json({"type": "TRANSCRIPT_PARTIAL", "text": event.text})
            elif event.type is VoiceEventType.INPUT_FINAL and event.text:
                await self._commit(
                    websocket,
                    {"turn_sequence": sequence, "text": event.text},
                    provider_id=event.provider_request_id or f"gemini-turn-{sequence}",
                )
                sequence += 1
            elif event.type is VoiceEventType.OUTPUT_TRANSCRIPT:
                await websocket.send_json({"type": "AGENT_TRANSCRIPT", "text": event.text})
            elif event.type is VoiceEventType.INTERRUPTED:
                await websocket.send_json({"type": "INTERRUPTED", "playback_cleared": True})
            elif event.type is VoiceEventType.DISCONNECTED:
                return

    async def _text_only(self, websocket: WebSocket) -> None:
        while True:
            try:
                value = json.loads(await websocket.receive_text())
            except WebSocketDisconnect:
                return
            if value.get("type") == "TEXT":
                await self._commit(
                    websocket,
                    value,
                    provider_id=str(value["provider_request_id"]),
                )
            elif value.get("type") == "END":
                return
            else:
                await websocket.send_json({"type": "ERROR", "code": "INVALID_CONTROL"})
