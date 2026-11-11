"""Event model, failure signatures and deduplication.

A signature is the fingerprint of a failure with the noise masked out
(numbers, hex, paths, timestamps), so the same bug restarting ten times maps
to one event whose count goes up.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Literal

Kind = Literal["crash", "suggestion", "info"]
Source = Literal["docker", "logs", "http", "agent"]

_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_PATH = re.compile(r"(?<![\w:])(?:~|\.{1,2})?(?:/[\w.\-]+)+/?")
_NUM = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")
_EXC_LINE = re.compile(
    r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Fault|Failure))\s*(?::\s?(.*))?$")
TRACEBACK = "Traceback (most recent call last):"


def normalise(text: str, limit: int = 200) -> str:
    text = _TS.sub("<ts>", text)
    text = _UUID.sub("<uuid>", text)
    text = _HEX.sub("<hex>", text)
    text = _PATH.sub("<path>", text)
    text = _NUM.sub("<n>", text)
    return _SPACE.sub(" ", text).strip()[:limit]


def find_exception(excerpt: str) -> tuple[str, str] | None:
    """Last `SomeError: message` line of a log excerpt, as (type, message)."""
    for line in reversed(excerpt.splitlines()):
        m = _EXC_LINE.match(line.strip())
        if m:
            return m.group(1), (m.group(2) or "").strip()
    return None


def signature_from_log(excerpt: str, exit_code: int | None = None) -> str:
    exc = find_exception(excerpt)
    if exc:
        etype, msg = exc
        return f"{etype}: {normalise(msg)}" if msg else etype
    if exit_code is not None:
        return f"exit:{exit_code}"
    last = next((ln for ln in reversed(excerpt.splitlines()) if ln.strip()), "")
    return f"log:{normalise(last)}"


def http_signature(url: str, status_class: str) -> str:
    return f"http:{url}:{status_class}"


def excerpt_of(lines: list[str], max_lines: int) -> str:
    """The lines before the exit, keeping the last traceback whole when it fits."""
    start = max(0, len(lines) - max_lines)
    for i in range(len(lines) - 1, -1, -1):
        if TRACEBACK in lines[i]:
            # Twice the budget for a whole traceback beats a truncated one: the
            # Analyst needs the frames, not just the last line.
            if len(lines) - i <= 2 * max_lines:
                start = min(start, max(0, i - 2))
            break
    return "\n".join(lines[start:])


@dataclass
class Event:
    kind: Kind
    source: Source
    signature: str
    message: str = ""
    status: str = ""
    exit_code: int | None = None
    log_excerpt: str = ""
    url: str | None = None
    http_status: int | None = None
    restart_loop: bool = False
    run_id: str | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    count: int = 1

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duplicates"] = self.count - 1
        return d


Listener = Callable[[Event, bool], None]
_UPDATABLE = ("message", "status", "exit_code", "log_excerpt", "url", "http_status")


class EventStore:
    """Thread-safe ring buffer of deduplicated events.

    Same (kind, signature) within the window since last_seen updates the
    existing event. A failure reported by several sources for one container
    run (log line + die event) counts once: `run_id` is the container's
    StartedAt, and only a new run bumps the count.
    """

    def __init__(self, windows: Callable[[], tuple[float, float]], maxlen: int = 200):
        self._windows = windows  # () -> (crash_window_s, suggestion_window_s)
        self._events: deque[Event] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []

    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def record(self, kind: Kind, source: Source, signature: str, *, run_id: str | None = None,
               now: float | None = None, **fields) -> tuple[Event, bool]:
        now = time.time() if now is None else now
        crash_w, suggestion_w = self._windows()
        window = suggestion_w if kind == "suggestion" else crash_w
        with self._lock:
            ev = next((e for e in reversed(self._events)
                       if e.kind == kind and e.signature == signature), None)
            if ev is not None and now - ev.last_seen <= window:
                if run_id is None or run_id != ev.run_id:
                    ev.count += 1
                ev.last_seen = now
                ev.run_id = run_id or ev.run_id
                for key in _UPDATABLE:
                    if fields.get(key) not in (None, ""):
                        setattr(ev, key, fields[key])
                if fields.get("restart_loop"):
                    ev.restart_loop = True
                is_new = False
            else:
                ev = Event(kind=kind, source=source, signature=signature, run_id=run_id,
                           first_seen=now, last_seen=now, **fields)
                self._events.append(ev)
                is_new = True
        for fn in list(self._listeners):
            fn(ev, is_new)
        return ev, is_new

    def mark_restart_loop(self, since: float, status: str) -> Event | None:
        """Flag the most recent crash since `since` as a restart loop."""
        with self._lock:
            ev = next((e for e in reversed(self._events)
                       if e.kind == "crash" and e.last_seen >= since and e.source != "http"), None)
            if ev is None or ev.restart_loop:
                return None
            ev.restart_loop = True
            ev.status = status
        for fn in list(self._listeners):
            fn(ev, False)
        return ev

    def list(self, limit: int = 100) -> list[Event]:
        with self._lock:
            return sorted(self._events, key=lambda e: e.last_seen, reverse=True)[:limit]

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            return next((e for e in self._events if e.id == event_id), None)

    def crashes_since(self, ts: float) -> list[Event]:
        with self._lock:
            return [e for e in self._events if e.kind == "crash" and e.last_seen >= ts]
