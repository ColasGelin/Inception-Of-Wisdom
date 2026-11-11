"""In-process pub/sub that feeds the SSE endpoint.

Observer threads publish from outside the event loop, so publishing always
goes through call_soon_threadsafe. Slow subscribers drop messages instead of
blocking the agent.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from collections import deque
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: set[asyncio.Queue[str]] = set()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subs.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        msg = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        try:
            self._loop.call_soon_threadsafe(self._fanout, msg)
        except RuntimeError:  # loop shutting down
            pass

    def _fanout(self, msg: str) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


bus = Bus()


class AgentLog:
    """The agent's own short `[component] message` lines: stdout, SSE, and hooks
    (the wisdom loop attaches one to capture the lines of the heal in progress)."""

    def __init__(self, maxlen: int = 500) -> None:
        self.lines: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self.hooks: list[Any] = []

    def say(self, component: str, message: str) -> dict[str, Any]:
        entry = {"ts": time.time(), "component": component, "message": message,
                 "text": f"[{component}] {message}"}
        with self._lock:  # threads print too: one write per line, never interleaved
            sys.stdout.write(entry["text"] + "\n")
            sys.stdout.flush()
        self.lines.append(entry)
        bus.publish("agent_log", entry)
        for hook in list(self.hooks):
            hook(entry)
        return entry


agent_log = AgentLog()
say = agent_log.say
