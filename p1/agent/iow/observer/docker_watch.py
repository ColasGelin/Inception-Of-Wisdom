"""Container side of the Observer.

The Docker SDK's events() and logs(stream=True) are blocking generators, so
each runs in its own daemon thread with its own retry loop. A missing
container or a Docker daemon that goes away only ever produces a backoff and
a status change, never an exception that reaches the agent.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import docker
from docker.errors import NotFound

from iow.bus import bus, say
from iow.config import ConfigStore
from iow.observer.events import EventStore, excerpt_of, signature_from_log

DOCKER_URL = "unix:///var/run/docker.sock"


@dataclass
class TargetState:
    container: str = ""
    state: str = "unknown"          # running | restarting | exited | created | missing | unknown
    exit_code: int | None = None
    started_at: str | None = None   # also the run id of the current container run
    restart_count: int = 0
    docker_ok: bool = False
    docker_error: str | None = None
    since: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Backoff:
    def __init__(self, max_s: Callable[[], float], first: float = 0.5):
        self._max, self._first, self._next = max_s, first, first

    def sleep(self) -> float:
        delay = self._next
        time.sleep(delay)
        self._next = min(self._next * 2, self._max())
        return delay

    def reset(self) -> None:
        self._next = self._first


def _parse_ts(ts: str) -> float:
    # Docker gives RFC3339 with nanoseconds; Python only parses microseconds.
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z?", ts)
    if not m:
        return time.time()
    frac = (m.group(2) or ".0")[:7]
    return datetime.fromisoformat(m.group(1) + frac).replace(tzinfo=timezone.utc).timestamp()


class LogBuffer:
    """Recent target log lines, tagged with the container run they belong to."""

    def __init__(self, maxlen: int = 1000):
        self._lines: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, ts: float, line: str, run_id: str | None) -> dict[str, Any]:
        entry = {"ts": ts, "line": line, "run_id": run_id}
        with self._lock:
            self._lines.append(entry)
        bus.publish("log", entry)
        return entry

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._lines)[-n:]

    def run_lines(self, run_id: str | None) -> list[str]:
        with self._lock:
            return [e["line"] for e in self._lines if e["run_id"] == run_id]


class DockerWatcher:
    def __init__(self, config: ConfigStore, events: EventStore,
                 client_factory: Callable[..., Any] | None = None):
        self.config = config
        self.events = events
        self.logs = LogBuffer()
        self.state = TargetState(container=config.get().target.container)
        self._client_factory = client_factory or (
            lambda timeout: docker.DockerClient(base_url=DOCKER_URL, timeout=timeout))
        self._starts: deque[float] = deque()
        self._seen: deque[tuple[str, str]] = deque(maxlen=2000)
        self._seen_set: set[tuple[str, str]] = set()
        self._pending_log_crash: threading.Timer | None = None
        self._last_crash_count: dict[str, int] = {}
        self._lock = threading.Lock()

    # ---- helpers -------------------------------------------------------
    @property
    def name(self) -> str:
        return self.config.get().target.container

    def _backoff(self) -> Backoff:
        return Backoff(lambda: self.config.get().observer.reconnect_backoff_max_s)

    def _set_state(self, **changes: Any) -> None:
        with self._lock:
            before = (self.state.state, self.state.exit_code, self.state.docker_ok,
                      self.state.started_at)
            for k, v in changes.items():
                setattr(self.state, k, v)
            self.state.container = self.name
            after = (self.state.state, self.state.exit_code, self.state.docker_ok,
                     self.state.started_at)
            changed = before != after
            if changed:
                self.state.since = time.time()
        if changed:
            if before[2] != after[2]:
                say("observer", "docker daemon reachable" if after[2]
                    else f"docker daemon unreachable: {self.state.docker_error}; retrying with backoff")
            if before[0] != after[0]:
                code = f" ({after[1]})" if after[0] == "exited" and after[1] is not None else ""
                say("observer", f"state: {after[0]}{code}")
            bus.publish("status", self.state.to_dict())

    def _docker_down(self, exc: Exception) -> None:
        self._set_state(docker_ok=False, docker_error=f"{type(exc).__name__}: {exc}"[:200],
                        state="unknown")

    def restart_target(self) -> None:
        """Restart (or start, if exited) the target container. Raises on failure."""
        stop_timeout = self.config.get().loop.restart_timeout_s
        client = self._client_factory(stop_timeout + 30)
        client.containers.get(self.name).restart(timeout=int(stop_timeout))

    def start(self) -> None:
        for fn, name in ((self._state_loop, "docker-state"), (self._events_loop, "docker-events"),
                         (self._logs_loop, "docker-logs")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    # ---- polled state (source of truth for /status) ---------------------
    def refresh_state(self, client: Any) -> None:
        try:
            c = client.containers.get(self.name)
        except NotFound:
            if self.state.state != "missing":
                self.events.record("info", "docker", f"missing:{self.name}",
                                   message=f"container {self.name} does not exist",
                                   status="missing")
            self._set_state(state="missing", docker_ok=True, docker_error=None)
            return
        st = c.attrs.get("State", {})
        status = "restarting" if st.get("Restarting") else st.get("Status", "unknown")
        self._set_state(state=status, exit_code=st.get("ExitCode"), started_at=st.get("StartedAt"),
                        restart_count=c.attrs.get("RestartCount", 0), docker_ok=True,
                        docker_error=None)

    def _state_loop(self) -> None:
        backoff = self._backoff()
        client = None
        while True:
            try:
                client = client or self._client_factory(10)
                self.refresh_state(client)
                backoff.reset()
                time.sleep(self.config.get().target.probe_interval_s)
            except Exception as exc:  # daemon gone, socket closed, timeouts
                self._docker_down(exc)
                client = None
                backoff.sleep()

    # ---- docker events ---------------------------------------------------
    def _events_loop(self) -> None:
        backoff = self._backoff()
        while True:
            try:
                # timeout=None: the events stream is legitimately silent for long periods.
                client = self._client_factory(None)
                client.ping()
                self._set_state(docker_ok=True, docker_error=None)
                backoff.reset()
                for ev in client.events(decode=True, filters={"type": "container",
                                                               "container": self.name}):
                    self.handle_docker_event(ev, client)
                # The stream only ends when the daemon closes it.
                raise ConnectionError("docker events stream closed")
            except Exception as exc:
                self._docker_down(exc)
                backoff.sleep()

    def handle_docker_event(self, ev: dict[str, Any], client: Any) -> None:
        action = (ev.get("Action") or ev.get("status") or "").split(":")[0]
        attrs = (ev.get("Actor") or {}).get("Attributes") or {}
        now = time.time()
        if action == "start":
            self._starts.append(now)
            window = self.config.get().observer.restart_loop
            while self._starts and now - self._starts[0] > window.window_s:
                self._starts.popleft()
            if len(self._starts) >= window.restarts:
                status = f"restart loop: {len(self._starts)} starts in {window.window_s:g}s"
                flagged = self.events.mark_restart_loop(now - window.window_s, status)
                if flagged is None and not any(
                        e.restart_loop for e in self.events.crashes_since(now - window.window_s)):
                    self.events.record("crash", "docker", f"restart-loop:{self.name}",
                                       message=status, status=status, restart_loop=True)
                if flagged is not None:
                    say("observer", f"{status} ({flagged.signature})")
        elif action == "die":
            code = int(attrs.get("exitCode", "0") or 0)
            if code != 0:
                self._crash_from_exit(client, code)
        if action in ("start", "die", "stop", "kill", "restart", "destroy", "create"):
            try:
                self.refresh_state(client)
            except Exception:
                pass

    def _crash_from_exit(self, client: Any, code: int) -> None:
        run_id, lines = None, []
        try:
            c = client.containers.get(self.name)
            run_id = c.attrs.get("State", {}).get("StartedAt")
            raw = c.logs(tail=400, stdout=True, stderr=True)
            lines = raw.decode("utf-8", "replace").splitlines()
        except Exception:
            lines = self.logs.run_lines(self.state.started_at)
        obs = self.config.get().observer
        excerpt = excerpt_of(lines, obs.log_excerpt_lines)
        sig = signature_from_log(excerpt, code)
        ev, new = self.events.record("crash", "docker", sig, run_id=run_id,
                                     message=f"container exited with code {code}",
                                     status=f"exited ({code})", exit_code=code,
                                     log_excerpt=excerpt)
        if new or ev.count != self._last_crash_count.get(sig):
            say("observer", f"crash: {sig} (exit {code})" + ("" if new else f", seen {ev.count}x"))
        self._last_crash_count[sig] = ev.count

    # ---- streamed logs ----------------------------------------------------
    def _logs_loop(self) -> None:
        backoff = self._backoff()
        since: int | None = None
        while True:
            try:
                client = self._client_factory(None)
                c = client.containers.get(self.name)
                run_id = c.attrs.get("State", {}).get("StartedAt")
                stream = c.logs(stream=True, follow=True, timestamps=True,
                                since=since if since is not None else int(time.time()) - 5)
                backoff.reset()
                buf = ""
                for chunk in stream:
                    buf += chunk.decode("utf-8", "replace")
                    *complete, buf = buf.split("\n")
                    for raw in complete:
                        ts = self._ingest(raw, run_id)
                        if ts is not None:
                            since = int(ts)
                # Stream ends when the container stops; poll until it runs again.
                time.sleep(min(1.0, self.config.get().target.probe_interval_s))
            except NotFound:
                backoff.sleep()
            except Exception as exc:
                self._docker_down(exc)
                backoff.sleep()

    def _ingest(self, raw: str, run_id: str | None) -> float | None:
        ts_text, _, line = raw.partition(" ")
        key = (ts_text, line)
        if not ts_text or key in self._seen_set:
            return None  # replay after reconnect: `since` has 1s resolution
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)
        ts = _parse_ts(ts_text)
        self.logs.add(ts, line, run_id)
        self.check_line(line, run_id)
        return ts

    def check_line(self, line: str, run_id: str | None) -> None:
        patterns = self.config.get().observer.error_patterns
        if not any(re.search(p, line) for p in patterns):
            return
        with self._lock:
            if self._pending_log_crash is not None:
                return  # one crash per burst: the timer reads the whole excerpt
            settle = self.config.get().observer.log_settle_s
            self._pending_log_crash = threading.Timer(settle, self._flush_log_crash, args=(run_id,))
            self._pending_log_crash.daemon = True
            self._pending_log_crash.start()

    def _flush_log_crash(self, run_id: str | None) -> None:
        with self._lock:
            self._pending_log_crash = None
        obs = self.config.get().observer
        excerpt = excerpt_of(self.logs.run_lines(run_id), obs.log_excerpt_lines)
        sig = signature_from_log(excerpt)
        ev, new = self.events.record("crash", "logs", sig, run_id=run_id,
                                     message="error pattern in target logs", log_excerpt=excerpt,
                                     status=self.state.state)
        if new:
            say("observer", f"crash: {sig} (from logs)")
