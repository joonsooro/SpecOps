"""Low-latency Gemini transport whose committed inputs use the V4 seam."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter
from specops_contracts import workshop_v1 as c

from specops_workshop.contracts import ConversationPhase, ProviderResumeContext
from specops_workshop.ports import LiveVoiceProvider, VoiceContext, VoiceEventType

from .orchestrator import FinalTranscriptInput, V4ProductionOrchestrator


VOICE_PROVIDER_CLOSE_TIMEOUT_SECONDS = 5.0
VOICE_PROVIDER_TASK_CANCEL_TIMEOUT_SECONDS = 5.0
_LOGGER = logging.getLogger("specops.workshop.voice")


def _consume_task_result(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    task.exception()


class V4LiveTransport:
    def __init__(
        self,
        provider: LiveVoiceProvider,
        orchestrator: V4ProductionOrchestrator,
        *,
        provider_close_timeout_seconds: float = VOICE_PROVIDER_CLOSE_TIMEOUT_SECONDS,
        provider_task_cancel_timeout_seconds: float = VOICE_PROVIDER_TASK_CANCEL_TIMEOUT_SECONDS,
    ) -> None:
        if provider_close_timeout_seconds <= 0 or provider_task_cancel_timeout_seconds <= 0:
            raise ValueError("provider teardown timeouts must be positive")
        self.provider = provider
        self.orchestrator = orchestrator
        self.provider_close_timeout_seconds = provider_close_timeout_seconds
        self.provider_task_cancel_timeout_seconds = provider_task_cancel_timeout_seconds

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        preparation = self.orchestrator.foundation.preparation_projection(
            self.orchestrator.case_id
        )
        runway = self.orchestrator.foundation.runway_projection(self.orchestrator.case_id)
        if preparation.get("workshop_complete_at") is not None:
            await websocket.send_json({"type": "ERROR", "code": "WORKSHOP_COMPLETE"})
            await websocket.close(code=4409)
            return
        if preparation["phase"] != "READY":
            await websocket.send_json(
                {"type": "ERROR", "code": "WORKSHOP_PREPARATION_NOT_READY"}
            )
            await websocket.close(code=4403)
            return
        await websocket.send_json({"type": "CALL_STATE", "state": "CONNECTING"})
        card = self.orchestrator.foundation.voice_session_card(self.orchestrator.case_id)
        try:
            session = await self.provider.connect(
                VoiceContext(
                    system_instruction=self._voice_instruction(card, runway),
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
            await self._cancel_pending_provider_tasks(pending)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            pass
        finally:
            await self._close_provider_session(session)

    async def _cancel_pending_provider_tasks(
        self, tasks: set[asyncio.Task]
    ) -> None:
        """Stop reading provider events without retaining the request handler."""

        if not tasks:
            return
        started = time.monotonic()
        correlation = str(self.orchestrator.case_id)

        def emit(event: str) -> None:
            _LOGGER.info(
                json.dumps(
                    {
                        "correlation_id": correlation,
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "event": event,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        emit("voice_provider_reader_cancel.started")
        for task in tasks:
            task.cancel()
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=self.provider_task_cancel_timeout_seconds
            )
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
                task.add_done_callback(_consume_task_result)
            emit("voice_provider_reader_cancel.cancelled")
            raise
        for task in done:
            _consume_task_result(task)
        if pending:
            for task in pending:
                task.add_done_callback(_consume_task_result)
            emit("voice_provider_reader_cancel.timed_out")
        else:
            emit("voice_provider_reader_cancel.completed")

    async def _close_provider_session(self, session) -> None:
        """Bound teardown only after the client/provider turn has ended.

        Model completion is never timed out here.  This bound prevents a
        provider SDK close handshake from retaining the WebSocket handler after
        Foundation has already stored the final transcript or the client has
        explicitly ended the call.
        """

        started = time.monotonic()
        correlation = str(self.orchestrator.case_id)

        def emit(event: str) -> None:
            _LOGGER.info(
                json.dumps(
                    {
                        "correlation_id": correlation,
                        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                        "event": event,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )

        emit("voice_provider_close.started")
        close_task = asyncio.create_task(session.close())

        try:
            done, _ = await asyncio.wait(
                (close_task,), timeout=self.provider_close_timeout_seconds
            )
        except asyncio.CancelledError:
            close_task.cancel()
            close_task.add_done_callback(_consume_task_result)
            emit("voice_provider_close.cancelled")
            raise
        if not done:
            close_task.cancel()
            close_task.add_done_callback(_consume_task_result)
            emit("voice_provider_close.timed_out")
            return
        try:
            close_task.result()
        except Exception:
            emit("voice_provider_close.failed")
        else:
            emit("voice_provider_close.completed")

    async def _commit(
        self, websocket: WebSocket, value: dict, *, provider_id: str, session=None
    ) -> bool:
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
        completion = await self.orchestrator.complete_from_final_transcript(
            receipt.transcript.transcript_event_id
        )
        if completion.action == "COMPLETE":
            assert completion.completion is not None
            await websocket.send_json(
                {
                    "type": "WORKSHOP_COMPLETE",
                    "state": completion.completion.state,
                    "replayed": completion.completion.replayed,
                }
            )
            return True
        if completion.action == "CONFIRMATION_REQUIRED":
            await websocket.send_json({"type": "COMPLETION_CONFIRMATION_REQUIRED"})
            if session is not None:
                await session.send_text(
                    "The PM may be signaling completion. Ask exactly: "
                    "‘Would you like me to finish the Spec Workshop now?’ "
                    "Do not ask a substantive question until the PM answers."
                )
            return False
        if session is not None:
            # This input-final callback is the conversational turn boundary.
            # Only a fresh Foundation read may update Voice guidance.
            card = self.orchestrator.foundation.voice_session_card(
                self.orchestrator.case_id
            )
            runway = self.orchestrator.foundation.runway_projection(
                self.orchestrator.case_id
            )
            await session.send_text(self._voice_instruction(card, runway))
        return False

    @staticmethod
    def _voice_instruction(card: c.VoiceSessionCard, runway: dict) -> str:
        if runway["depth"] == 0:
            return (
                "No substantive clarification question is currently admitted. "
                "Summarize admitted information, invite corrections, and say exactly: "
                "‘I’m preparing the next clarification area. You can correct anything already captured while I do that.’"
            )
        allowed = "\n".join(
            f"{index + 1}. {item['exact_text']}" for index, item in enumerate(runway["questions"])
        )
        return (
            "Facilitate the Workshop without interpreting confirmations. Ask only one of the "
            "following Foundation-admitted questions, in order; do not invent, revise, or combine them. "
            "A clear PM statement that the Spec Workshop is complete ends the Workshop; an ambiguous "
            "completion statement requires one direct confirmation before any further substantive question. "
            f"Runway health: {card.runway_health.value}.\n{allowed}"
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
                completed = await self._commit(
                    websocket,
                    value,
                    provider_id=str(value["provider_request_id"]),
                    session=session,
                )
                if completed:
                    return
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
                completed = await self._commit(
                    websocket,
                    {"turn_sequence": sequence, "text": event.text},
                    provider_id=event.provider_request_id or f"gemini-turn-{sequence}",
                    session=session,
                )
                sequence += 1
                if completed:
                    return
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
                completed = await self._commit(
                    websocket,
                    value,
                    provider_id=str(value["provider_request_id"]),
                )
                if completed:
                    return
            elif value.get("type") == "END":
                return
            else:
                await websocket.send_json({"type": "ERROR", "code": "INVALID_CONTROL"})
