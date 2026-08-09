from __future__ import annotations

import asyncio
import json
from collections import deque
from uuid import UUID

from fastapi import WebSocket, WebSocketDisconnect
from specops_workflow.errors import DomainError, ErrorCode
from specops_workflow.models import QueryOne

from .contracts import CallState, ConversationPhase, ProviderResumeContext
from .analyzer import AnalyzerTurnResult, ControlIntent, ControlTarget
from .orchestration import WorkshopCoordinator
from .ports import LiveVoiceProvider, LiveVoiceSession, VoiceContext, VoiceEventType
from .sessions import WorkshopStore


INPUT_AUDIO_LIMIT = 320_000
OUTPUT_AUDIO_LIMIT = 480_000
PCM16_16K_FRAME_BYTES = 640
IDLE_TIMEOUT_SECONDS = 30 * 60


class BoundedAudioBuffer:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._chunks: deque[bytes] = deque()
        self.byte_length = 0

    def push(self, chunk: bytes) -> None:
        if not chunk:
            return
        if len(chunk) > self.max_bytes:
            chunk = chunk[-self.max_bytes:]
        self._chunks.append(bytes(chunk))
        self.byte_length += len(chunk)
        while self.byte_length > self.max_bytes and self._chunks:
            removed = self._chunks.popleft()
            self.byte_length -= len(removed)

    def pop(self) -> bytes | None:
        if not self._chunks:
            return None
        value = self._chunks.popleft()
        self.byte_length -= len(value)
        return value

    def clear(self) -> None:
        self._chunks.clear()
        self.byte_length = 0


class LiveTransport:
    def __init__(
        self,
        provider: LiveVoiceProvider,
        coordinator: WorkshopCoordinator,
        store: WorkshopStore,
        *,
        session_id: UUID,
        idle_timeout_seconds: float = IDLE_TIMEOUT_SECONDS,
        gate=None,
        finish_coordinator=None,
    ) -> None:
        self.provider = provider
        self.coordinator = coordinator
        self.store = store
        self.session_id = session_id
        if idle_timeout_seconds <= 0:
            raise ValueError("idle timeout must be positive")
        self.idle_timeout_seconds = idle_timeout_seconds
        self._last_input_at = 0.0
        self.gate = gate
        self._generation_blocked = False
        self.finish_coordinator = finish_coordinator

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        recovered = self.coordinator.recover(self.session_id)
        query = QueryOne(
            case_id=recovered.session.case_id,
            acting_actor_id=recovered.session.pm_actor_id,
        )
        try:
            committed_package = self.coordinator.foundation.get_spec_package_content(query)
        except DomainError as exc:
            if exc.code != ErrorCode.RECORD_NOT_FOUND:
                raise
            committed_package = None
        downstream_handoff = None
        if recovered.session.conversation_phase == ConversationPhase.HANDOFF_READY:
            downstream_handoff = self.coordinator.foundation.get_downstream_handoff(query)
        context = VoiceContext(
            system_instruction=(
                "You are the SpecOps Workshop facilitator. Respect the PM business authority, "
                "the Dev Lead technical delegation, and only discuss evidence-backed package formulation."
            ),
            resume=ProviderResumeContext(
                conversation_phase=recovered.session.conversation_phase,
                committed_package=committed_package,
                downstream_handoff=downstream_handoff,
                final_transcript_snapshots=self.store.all_snapshots(self.session_id),
            ),
        )
        session = await self._connect_with_retry(websocket, context)
        if session is None:
            await self._text_only(websocket)
            return
        self.store.update_phase(self.session_id, call_state=CallState.LISTENING, now=self.coordinator.clock.now())
        await websocket.send_json({"type": "CALL_STATE", "state": CallState.LISTENING.value})
        self._last_input_at = asyncio.get_running_loop().time()
        client_task = asyncio.create_task(self._client_to_provider(websocket, session))
        provider_task = asyncio.create_task(self._provider_to_client(websocket, session))
        idle_task = asyncio.create_task(self._expire_idle(websocket))
        try:
            done, pending = await asyncio.wait((client_task, provider_task, idle_task), return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        except WebSocketDisconnect:
            self.store.update_phase(self.session_id, call_state=CallState.DISCONNECTED, now=self.coordinator.clock.now())
        finally:
            await session.close()

    async def _connect_with_retry(self, websocket: WebSocket, context: VoiceContext) -> LiveVoiceSession | None:
        self.store.update_phase(self.session_id, call_state=CallState.CONNECTING, now=self.coordinator.clock.now())
        await websocket.send_json({"type": "CALL_STATE", "state": CallState.CONNECTING.value})
        for delay in (0.0, 0.25, 0.75):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.provider.connect(context)
            except Exception:
                continue
        self.store.update_phase(self.session_id, call_state=CallState.DISCONNECTED, now=self.coordinator.clock.now())
        await websocket.send_json({
            "type": "CALL_STATE", "state": CallState.DISCONNECTED.value,
            "guidance": "Voice is unavailable. Continue with text; your final turns remain recoverable.",
        })
        return None

    async def _client_to_provider(self, websocket: WebSocket, session: LiveVoiceSession) -> None:
        audio = BoundedAudioBuffer(INPUT_AUDIO_LIMIT)
        while True:
            message = await websocket.receive()
            self._last_input_at = asyncio.get_running_loop().time()
            if message.get("bytes") is not None:
                frame = message["bytes"]
                if len(frame) != PCM16_16K_FRAME_BYTES:
                    await websocket.send_json({"type": "ERROR", "code": "INVALID_AUDIO_FRAME"})
                    continue
                audio.push(frame)
                current = audio.pop()
                if current is not None:
                    await session.send_audio(current)
                continue
            if message.get("text") is None:
                self.store.update_phase(
                    self.session_id, call_state=CallState.DISCONNECTED, now=self.coordinator.clock.now()
                )
                return
            value = json.loads(message["text"])
            kind = value.get("type")
            if kind == "TEXT":
                result = self.coordinator.commit_final_turn(
                    self.session_id, turn_sequence=int(value["turn_sequence"]), text=str(value["text"]),
                    provider_request_id=str(value["provider_request_id"]),
                    correction_of_version=value.get("correction_of_version"),
                )
                proposal = None
                if self.gate is not None:
                    proposal = await self.gate.analyze_final_turn(
                        self.session_id, int(value["turn_sequence"])
                    )
                await websocket.send_json({"type": "FINAL_COMMITTED", "revision": result.receipt.revision})
                if proposal is not None and proposal.complete_package_proposal is not None:
                    self._generation_blocked = True
                    await session.interrupt()
                    pending = self.store.pending_proposal(self.session_id)
                    await websocket.send_json({
                        "type": "PROPOSAL_PENDING",
                        "proposal_ref": pending.proposal_ref if pending else None,
                        "acknowledgement": proposal.acknowledgement,
                        "next_question": proposal.next_question,
                    })
                elif proposal is not None and proposal.control_intent == ControlIntent.FINISH:
                    if self.finish_coordinator is None:
                        await websocket.send_json({"type": "ERROR", "code": "FINISH_UNAVAILABLE"})
                    else:
                        finished = await self.finish_coordinator.finish(self.session_id)
                        await websocket.send_json({"type": "FINISH_COMPLETE", "handoff": finished.handoff.model_dump(mode="json")})
                        await session.send_text(
                            "HANDOFF_READY. Summarize only these committed handoff facts: "
                            + finished.handoff.model_dump_json()
                        )
                else:
                    await session.send_text(str(value["text"]))
            elif kind == "FINISH":
                if self.finish_coordinator is None:
                    await websocket.send_json({"type": "ERROR", "code": "FINISH_UNAVAILABLE"})
                    continue
                finished = await self.finish_coordinator.finish(self.session_id)
                await websocket.send_json({"type": "FINISH_COMPLETE", "handoff": finished.handoff.model_dump(mode="json")})
                await session.send_text(
                    "HANDOFF_READY. Summarize only these committed handoff facts: "
                    + finished.handoff.model_dump_json()
                )
            elif kind == "CONTROL" and self.gate is not None:
                snapshots = self.store.latest_snapshots(self.session_id)
                if not snapshots:
                    await websocket.send_json({"type": "ERROR", "code": "NO_FINAL_EVIDENCE"})
                    continue
                intent = ControlIntent(str(value.get("intent")))
                control = AnalyzerTurnResult(
                    schema_version=1,
                    turn_source_ref=snapshots[-1].final_source_ref,
                    finding_proposals=[],
                    complete_package_proposal=None,
                    control_intent=intent,
                    control_target=ControlTarget.WORKSHOP_PATCH,
                    target_proposal_ref=str(value.get("proposal_ref")),
                    edit_instruction=value.get("edit_instruction"),
                    acknowledgement=value.get("acknowledgement"),
                    next_question=None,
                )
                applied = self.gate.apply_control(
                    self.session_id,
                    control,
                    confirmation_context=self._generation_blocked,
                )
                if intent == ControlIntent.EDIT:
                    await self.gate.analyze_final_turn(
                        self.session_id,
                        snapshots[-1].turn_sequence,
                        edit_instruction=value.get("edit_instruction"),
                    )
                self._generation_blocked = False
                await websocket.send_json({
                    "type": "CONTROL_APPLIED",
                    "intent": intent.value,
                    "status": None if applied is None else applied.status.value,
                })
                if intent == ControlIntent.CONFIRM:
                    await session.send_text(
                        "The proposed package is committed. Continue with one focused question."
                    )
            elif kind == "INTERRUPT":
                audio.clear()
                await websocket.send_json({"type": "INTERRUPTED", "playback_cleared": True})
                await session.interrupt()
            elif kind == "END":
                self.store.update_phase(
                    self.session_id, call_state=CallState.ENDED, conversation_phase=ConversationPhase.COMPLETE,
                    now=self.coordinator.clock.now(),
                )
                await websocket.send_json({"type": "CALL_STATE", "state": CallState.ENDED.value})
                return
            else:
                await websocket.send_json({"type": "ERROR", "code": "INVALID_CONTROL"})

    async def _expire_idle(self, websocket: WebSocket) -> None:
        while True:
            remaining = self.idle_timeout_seconds - (asyncio.get_running_loop().time() - self._last_input_at)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            session = self.store.get_session(self.session_id)
            self.store.update_phase(
                self.session_id, call_state=CallState.DISCONNECTED, now=self.coordinator.clock.now()
            )
            await websocket.send_json({
                "type": "CALL_STATE", "state": CallState.DISCONNECTED.value,
                "phase": session.conversation_phase.value,
                "guidance": "Voice expired after 30 minutes without input. Reconnect or continue with text.",
            })
            return

    async def _provider_to_client(self, websocket: WebSocket, session: LiveVoiceSession) -> None:
        output = BoundedAudioBuffer(OUTPUT_AUDIO_LIMIT)
        next_sequence = len(self.store.latest_snapshots(self.session_id)) + 1
        async for event in session.events():
            if event.type == VoiceEventType.AUDIO and event.audio is not None:
                if self._generation_blocked:
                    continue
                output.push(event.audio)
                chunk = output.pop()
                if chunk is not None:
                    await websocket.send_bytes(chunk)
            elif event.type == VoiceEventType.INPUT_PARTIAL:
                await websocket.send_json({"type": "TRANSCRIPT_PARTIAL", "text": event.text})
            elif event.type == VoiceEventType.INPUT_FINAL and event.text:
                result = self.coordinator.commit_final_turn(
                    self.session_id, turn_sequence=next_sequence, text=event.text,
                    provider_request_id=event.provider_request_id or f"provider-turn-{next_sequence}",
                )
                if self.gate is not None:
                    proposal = await self.gate.analyze_final_turn(self.session_id, next_sequence)
                    if proposal.complete_package_proposal is not None:
                        self._generation_blocked = True
                        await session.interrupt()
                await websocket.send_json({
                    "type": "TRANSCRIPT_FINAL", "text": event.text,
                    "turn_sequence": next_sequence, "revision": result.receipt.revision,
                })
                if self._generation_blocked:
                    pending = self.store.pending_proposal(self.session_id)
                    await websocket.send_json({
                        "type": "PROPOSAL_PENDING",
                        "proposal_ref": pending.proposal_ref if pending else None,
                        "acknowledgement": proposal.acknowledgement,
                        "next_question": proposal.next_question,
                    })
                elif self.finish_coordinator is not None and proposal.control_intent == ControlIntent.FINISH:
                    finished = await self.finish_coordinator.finish(self.session_id)
                    await websocket.send_json({"type": "FINISH_COMPLETE", "handoff": finished.handoff.model_dump(mode="json")})
                    await session.send_text(
                        "HANDOFF_READY. Summarize only these committed handoff facts: "
                        + finished.handoff.model_dump_json()
                    )
                next_sequence += 1
            elif event.type == VoiceEventType.OUTPUT_TRANSCRIPT:
                if self._generation_blocked:
                    continue
                await websocket.send_json({"type": "AGENT_TRANSCRIPT", "text": event.text})
            elif event.type == VoiceEventType.INTERRUPTED:
                output.clear()
                await websocket.send_json({"type": "INTERRUPTED", "playback_cleared": True})

    async def _text_only(self, websocket: WebSocket) -> None:
        while True:
            try:
                value = json.loads(await websocket.receive_text())
            except WebSocketDisconnect:
                return
            if value.get("type") == "TEXT":
                result = self.coordinator.commit_final_turn(
                    self.session_id, turn_sequence=int(value["turn_sequence"]), text=str(value["text"]),
                    provider_request_id=str(value["provider_request_id"]),
                    correction_of_version=value.get("correction_of_version"),
                )
                proposal = None
                if self.gate is not None:
                    proposal = await self.gate.analyze_final_turn(
                        self.session_id, int(value["turn_sequence"])
                    )
                await websocket.send_json({"type": "FINAL_COMMITTED", "revision": result.receipt.revision})
                if (
                    proposal is not None
                    and proposal.control_intent == ControlIntent.FINISH
                    and self.finish_coordinator is not None
                ):
                    finished = await self.finish_coordinator.finish(self.session_id)
                    await websocket.send_json({
                        "type": "FINISH_COMPLETE",
                        "handoff": finished.handoff.model_dump(mode="json"),
                    })
            elif value.get("type") == "FINISH":
                if self.finish_coordinator is None:
                    await websocket.send_json({"type": "ERROR", "code": "FINISH_UNAVAILABLE"})
                    continue
                finished = await self.finish_coordinator.finish(self.session_id)
                await websocket.send_json({
                    "type": "FINISH_COMPLETE",
                    "handoff": finished.handoff.model_dump(mode="json"),
                })
            elif value.get("type") == "END":
                self.store.update_phase(
                    self.session_id, call_state=CallState.ENDED, conversation_phase=ConversationPhase.COMPLETE,
                    now=self.coordinator.clock.now(),
                )
                await websocket.send_json({"type": "CALL_STATE", "state": CallState.ENDED.value})
                return
            else:
                await websocket.send_json({"type": "ERROR", "code": "INVALID_CONTROL"})
