"""In-process wake-ups for live Server-Sent Event connections."""

import asyncio


class NotificationHub:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def event_for(self, session_id: str) -> asyncio.Event:
        async with self._lock:
            event = self._events.get(session_id)
            if event is None:
                event = asyncio.Event()
                self._events[session_id] = event
            return event

    async def notify(self, session_id: str) -> None:
        event = await self.event_for(session_id)
        event.set()
