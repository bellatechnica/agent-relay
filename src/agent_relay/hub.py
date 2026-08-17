"""In-process wake-ups for live Server-Sent Event connections."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class NotificationHub:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._active_mcp_waits: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _event_for_locked(self, session_id: str) -> asyncio.Event:
        event = self._events.get(session_id)
        if event is None:
            event = asyncio.Event()
            self._events[session_id] = event
        return event

    async def event_for(self, session_id: str) -> asyncio.Event:
        async with self._lock:
            return self._event_for_locked(session_id)

    @asynccontextmanager
    async def mcp_wait(self, session_id: str) -> AsyncIterator[asyncio.Event]:
        async with self._lock:
            event = self._event_for_locked(session_id)
            self._active_mcp_waits[session_id] = (
                self._active_mcp_waits.get(session_id, 0) + 1
            )
        try:
            yield event
        finally:
            async with self._lock:
                remaining = self._active_mcp_waits[session_id] - 1
                if remaining == 0:
                    del self._active_mcp_waits[session_id]
                else:
                    self._active_mcp_waits[session_id] = remaining

    async def is_mcp_waiting(self, session_id: str) -> bool:
        async with self._lock:
            return self._active_mcp_waits.get(session_id, 0) > 0

    async def notify(self, session_id: str) -> bool:
        async with self._lock:
            recipient_waiting = self._active_mcp_waits.get(session_id, 0) > 0
            self._event_for_locked(session_id).set()
            return recipient_waiting
