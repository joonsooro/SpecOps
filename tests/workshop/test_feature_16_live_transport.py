from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from specops_workflow import FrozenClock
from specops_workshop.legacy_test_app import DEMO_SESSION_ID, create_app
from specops_workshop.config import GEMINI_MODEL, TERRA_MODEL, Settings
from specops_workshop.contracts import CallState, ConversationPhase
from specops_workshop.live_transport import BoundedAudioBuffer, INPUT_AUDIO_LIMIT, OUTPUT_AUDIO_LIMIT
from specops_workshop.ports import VoiceEvent, VoiceEventType
from specops_workshop.sources import SourceCatalog


ROOT = Path("/Users/rudinro/Desktop/SpecOps/Spec_Eng")
NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


def configured(tmp_path: Path) -> Settings:
    return Settings.load({
        "SPECOPS_DATABASE_URL": f"sqlite:///{tmp_path / 'foundation.sqlite'}",
        "WORKSHOP_DATABASE_URL": f"sqlite:///{tmp_path / 'workshop.sqlite'}",
        "GEMINI_API_KEY": "gemini-test-value", "OPENAI_API_KEY": "openai-test-value",
        "GEMINI_LIVE_MODEL": GEMINI_MODEL, "OPENAI_ANALYZER_MODEL": TERRA_MODEL,
    })


class FakeSession:
    def __init__(self, initial_events: tuple[VoiceEvent, ...] = ()) -> None:
        self.initial_events = initial_events
        self.audio: list[bytes] = []
        self.text: list[str] = []
        self.interruptions = 0
        self.closed = False

    async def send_audio(self, frame: bytes) -> None:
        self.audio.append(frame)

    async def send_text(self, text: str) -> None:
        self.text.append(text)

    async def interrupt(self) -> None:
        self.interruptions += 1

    async def events(self):
        for event in self.initial_events:
            yield event
        while not self.closed:
            await asyncio.sleep(0.01)

    async def close(self) -> None:
        self.closed = True


class FakeProvider:
    def __init__(self, session: FakeSession, failures: int = 0) -> None:
        self.session = session
        self.failures = failures
        self.connect_calls = 0
        self.contexts = []

    async def connect(self, context):
        self.connect_calls += 1
        self.contexts.append(context)
        if self.connect_calls <= self.failures:
            raise ConnectionError("fixture provider unavailable")
        return self.session


def test_binary_audio_text_interrupt_and_end_share_one_live_session(tmp_path):
    session = FakeSession()
    provider = FakeProvider(session)
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(ROOT), live_provider=provider,
    )
    with TestClient(app).websocket_connect("/ws/live") as websocket:
        assert websocket.receive_json() == {"type": "CALL_STATE", "state": "CONNECTING"}
        assert websocket.receive_json() == {"type": "CALL_STATE", "state": "LISTENING"}
        frame = b"\x00\x01" * 320
        websocket.send_bytes(frame)
        websocket.send_text('{"type":"TEXT","text":"Keep all filtered rows.","turn_sequence":1,"provider_request_id":"text-1"}')
        committed = websocket.receive_json()
        assert committed["type"] == "FINAL_COMMITTED"
        app.state.workshop_store.update_phase(
            DEMO_SESSION_ID, conversation_phase=ConversationPhase.HANDOFF_READY,
            call_state=CallState.LISTENING, now=NOW,
        )
        assert session.closed is False
        assert app.state.workshop_store.get_session(DEMO_SESSION_ID).conversation_phase == ConversationPhase.HANDOFF_READY
        websocket.send_text('{"type":"INTERRUPT"}')
        assert websocket.receive_json() == {"type": "INTERRUPTED", "playback_cleared": True}
        websocket.send_text('{"type":"END"}')
        assert websocket.receive_json() == {"type": "CALL_STATE", "state": "ENDED"}
    assert session.audio == [frame]
    assert session.text == ["Keep all filtered rows."]
    assert session.interruptions == 1 and session.closed is True
    recovered = app.state.coordinator.recover(DEMO_SESSION_ID)
    assert recovered.final_transcripts[0].normalized_text == "Keep all filtered rows."
    assert recovered.session.call_state == CallState.ENDED
    assert recovered.session.conversation_phase == ConversationPhase.COMPLETE


def test_three_failed_connects_keep_text_fallback_active(tmp_path):
    provider = FakeProvider(FakeSession(), failures=3)
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(ROOT), live_provider=provider,
    )
    with TestClient(app).websocket_connect("/ws/live") as websocket:
        assert websocket.receive_json()["state"] == "CONNECTING"
        disconnected = websocket.receive_json()
        assert disconnected["state"] == "DISCONNECTED" and "text" in disconnected["guidance"].lower()
        websocket.send_text('{"type":"TEXT","text":"Use UTF-8 CSV.","turn_sequence":1,"provider_request_id":"fallback-1"}')
        assert websocket.receive_json()["type"] == "FINAL_COMMITTED"
        websocket.send_text('{"type":"END"}')
        assert websocket.receive_json()["state"] == "ENDED"
    assert provider.connect_calls == 3


def test_provider_partial_is_display_only_and_discarded_on_disconnect(tmp_path):
    provider = FakeProvider(FakeSession((VoiceEvent(VoiceEventType.INPUT_PARTIAL, text="not final"),)))
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(ROOT), live_provider=provider,
    )
    with TestClient(app).websocket_connect("/ws/live") as websocket:
        websocket.receive_json()
        websocket.receive_json()
        assert websocket.receive_json() == {"type": "TRANSCRIPT_PARTIAL", "text": "not final"}
        websocket.send_text('{"type":"END"}')
        assert websocket.receive_json()["state"] == "ENDED"
    assert app.state.workshop_store.latest_snapshots(DEMO_SESSION_ID) == ()


def test_audio_buffers_are_hard_bounded_and_never_touch_disk(tmp_path):
    incoming = BoundedAudioBuffer(INPUT_AUDIO_LIMIT)
    outgoing = BoundedAudioBuffer(OUTPUT_AUDIO_LIMIT)
    incoming.push(b"i" * (INPUT_AUDIO_LIMIT + 640))
    outgoing.push(b"o" * (OUTPUT_AUDIO_LIMIT + 960))
    assert incoming.byte_length == INPUT_AUDIO_LIMIT
    assert outgoing.byte_length == OUTPUT_AUDIO_LIMIT
    incoming.clear(); outgoing.clear()
    assert incoming.byte_length == outgoing.byte_length == 0
    assert list(tmp_path.rglob("*.pcm")) == []
    assert list(tmp_path.rglob("*.wav")) == []


def test_thirty_minute_idle_expiry_closes_provider_but_retains_phase(tmp_path):
    session = FakeSession()
    provider = FakeProvider(session)
    app = create_app(
        settings=configured(tmp_path), clock=FrozenClock(NOW), source_catalog=SourceCatalog(ROOT),
        live_provider=provider, idle_timeout_seconds=0.02,
    )
    with TestClient(app).websocket_connect("/ws/live") as websocket:
        websocket.receive_json()
        websocket.receive_json()
        expired = websocket.receive_json()
        assert expired["state"] == "DISCONNECTED"
        assert expired["phase"] == "WORKSHOP"
        assert "30 minutes" in expired["guidance"]
    stored = app.state.workshop_store.get_session(DEMO_SESSION_ID)
    assert stored.call_state == CallState.DISCONNECTED
    assert stored.conversation_phase == ConversationPhase.WORKSHOP
    assert session.closed is True
