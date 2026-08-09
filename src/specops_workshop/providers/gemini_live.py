from __future__ import annotations

from typing import AsyncIterator
from uuid import uuid4

from google import genai
from google.genai import types

from ..config import GEMINI_MODEL
from ..ports import LiveVoiceSession, VoiceContext, VoiceEvent, VoiceEventType


class GeminiLiveProvider:
    """Server-only adapter for the official Google GenAI Live SDK."""

    def __init__(self, *, api_key: str, model: str = GEMINI_MODEL) -> None:
        if model != GEMINI_MODEL:
            raise ValueError("Gemini Live model must match the pinned Workshop model")
        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def connect(self, context: VoiceContext) -> LiveVoiceSession:
        config = {
            "response_modalities": ["AUDIO"],
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            "system_instruction": context.system_instruction,
        }
        manager = self._client.aio.live.connect(model=self._model, config=config)
        session = await manager.__aenter__()
        wrapped = _GeminiLiveSession(manager, session)
        resume = context.resume
        if (
            resume.committed_package is not None
            or resume.downstream_handoff is not None
            or resume.final_transcript_snapshots
        ):
            await wrapped.send_text(
                "AUTHORITATIVE_RESUME_CONTEXT\n" + resume.model_dump_json()
            )
        return wrapped


class _GeminiLiveSession:
    def __init__(self, manager, session) -> None:
        self._manager = manager
        self._session = session
        self._closed = False
        self._input_transcript: list[str] = []
        self._request_id = str(uuid4())

    async def send_audio(self, frame: bytes) -> None:
        await self._session.send_realtime_input(
            audio=types.Blob(data=frame, mime_type="audio/pcm;rate=16000")
        )

    async def send_text(self, text: str) -> None:
        await self._session.send_realtime_input(text=text)

    async def interrupt(self) -> None:
        # Gemini automatic VAD performs provider-side cancellation when the
        # participant's next audio arrives. Browser playback is cleared first.
        self._input_transcript.clear()

    async def events(self) -> AsyncIterator[VoiceEvent]:
        async for response in self._session.receive():
            content = getattr(response, "server_content", None)
            if content is None:
                continue
            transcription = getattr(content, "input_transcription", None)
            if transcription is not None and getattr(transcription, "text", None):
                self._input_transcript.append(transcription.text)
                yield VoiceEvent(VoiceEventType.INPUT_PARTIAL, text="".join(self._input_transcript), provider_request_id=self._request_id)
            output = getattr(content, "output_transcription", None)
            if output is not None and getattr(output, "text", None):
                yield VoiceEvent(VoiceEventType.OUTPUT_TRANSCRIPT, text=output.text, provider_request_id=self._request_id)
            model_turn = getattr(content, "model_turn", None)
            if model_turn is not None:
                for part in getattr(model_turn, "parts", ()):
                    inline = getattr(part, "inline_data", None)
                    if inline is not None and getattr(inline, "data", None):
                        yield VoiceEvent(VoiceEventType.AUDIO, audio=bytes(inline.data), provider_request_id=self._request_id)
            if getattr(content, "interrupted", False):
                self._input_transcript.clear()
                yield VoiceEvent(VoiceEventType.INTERRUPTED, provider_request_id=self._request_id)
            if getattr(content, "turn_complete", False):
                if self._input_transcript:
                    yield VoiceEvent(
                        VoiceEventType.INPUT_FINAL,
                        text="".join(self._input_transcript),
                        provider_request_id=self._request_id,
                    )
                    self._input_transcript.clear()
                yield VoiceEvent(VoiceEventType.TURN_COMPLETE, provider_request_id=self._request_id)
                self._request_id = str(uuid4())

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._manager.__aexit__(None, None, None)
