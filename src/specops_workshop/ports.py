from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import AsyncIterator, Protocol

from .contracts import ProviderResumeContext


class VoiceEventType(StrEnum):
    AUDIO = "AUDIO"
    INPUT_PARTIAL = "INPUT_PARTIAL"
    INPUT_FINAL = "INPUT_FINAL"
    OUTPUT_TRANSCRIPT = "OUTPUT_TRANSCRIPT"
    INTERRUPTED = "INTERRUPTED"
    TURN_COMPLETE = "TURN_COMPLETE"
    DISCONNECTED = "DISCONNECTED"


@dataclass(frozen=True)
class VoiceContext:
    system_instruction: str
    resume: ProviderResumeContext


@dataclass(frozen=True)
class VoiceEvent:
    type: VoiceEventType
    audio: bytes | None = None
    text: str | None = None
    provider_request_id: str | None = None


class LiveVoiceSession(Protocol):
    async def send_audio(self, frame: bytes) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def interrupt(self) -> None: ...
    def events(self) -> AsyncIterator[VoiceEvent]: ...
    async def close(self) -> None: ...


class LiveVoiceProvider(Protocol):
    async def connect(self, context: VoiceContext) -> LiveVoiceSession: ...
