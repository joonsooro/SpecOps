"""Browser-page presence independent of the optional Gemini Voice session."""

from __future__ import annotations

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from .orchestrator import V4ProductionOrchestrator


class WorkshopClientPresence:
    """Start retention grace only when the final Workshop page disconnects."""

    def __init__(self, orchestrator: V4ProductionOrchestrator) -> None:
        self.orchestrator = orchestrator
        self._connections: set[int] = set()
        self._lock = asyncio.Lock()

    def has_active_clients(self) -> bool:
        return bool(self._connections)

    async def handle(self, websocket: WebSocket) -> None:
        await websocket.accept()
        identity = id(websocket)
        async with self._lock:
            first = not self._connections
            self._connections.add(identity)
            if first:
                await self.orchestrator.note_client_connected()
        try:
            while True:
                await websocket.receive()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            async with self._lock:
                self._connections.discard(identity)
                last = not self._connections
                if last:
                    disconnected_at = self.orchestrator.now()
                    await self.orchestrator.note_last_client_disconnected(
                        disconnected_at=disconnected_at
                    )
