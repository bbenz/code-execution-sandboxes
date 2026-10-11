"""Per-run event log with live fan-out to server-sent-event subscribers."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator


class RunState:
    def __init__(self, run_id: str, kind: str, request: dict):
        self.run_id = run_id
        self.kind = kind
        self.request = request
        self.events: list[dict] = []
        self.done = False
        self.result: dict | None = None
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, event_type: str, **data) -> dict:
        event = {"seq": len(self.events) + 1, "type": event_type, "ts": time.time(), "run_id": self.run_id, **data}
        self.events.append(event)
        for queue in list(self._subscribers):
            queue.put_nowait(event)
        return event

    def finish(self, result: dict) -> None:
        self.result = result
        self.done = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    async def stream(self, after: int = 0, keepalive_s: float = 15.0) -> AsyncIterator[dict | None]:
        """Yield past events after `after`, then live events. Yields None as a keep-alive tick."""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            for event in list(self.events):
                if event["seq"] > after:
                    yield event
                    after = event["seq"]
            if self.done:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
                except TimeoutError:
                    yield None
                    continue
                if event is None:
                    return
                if event["seq"] > after:
                    yield event
                    after = event["seq"]
        finally:
            self._subscribers.discard(queue)


class RunRegistry:
    def __init__(self, keep: int = 25):
        self._runs: dict[str, RunState] = {}
        self._keep = keep

    def create(self, kind: str, request: dict) -> RunState:
        run_id = f"{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
        state = RunState(run_id, kind, request)
        self._runs[run_id] = state
        while len(self._runs) > self._keep:
            self._runs.pop(next(iter(self._runs)))
        return state

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def latest(self) -> RunState | None:
        return next(reversed(self._runs.values()), None) if self._runs else None
