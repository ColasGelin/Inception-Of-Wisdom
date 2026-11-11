"""HTTP side of the Observer: periodic probes of the configured URLs.

Refused / timeout / 5xx are crashes, 4xx are suggestions (shown, never
patched). A probe failure only counts as a crash when the container has been
running the same run for N consecutive probes: while the container is down
or restarting, the Docker side already owns the failure, and counting it
twice would turn one bad start into two events.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from iow.bus import bus, say
from iow.config import ConfigStore
from iow.observer.docker_watch import DockerWatcher
from iow.observer.events import EventStore, http_signature


def classify(status: int | None, error: str | None) -> str:
    if error:
        return error  # "refused" | "timeout" | "error"
    assert status is not None
    if status >= 500:
        return "5xx"
    if status >= 400:
        return "4xx"
    return "ok"


async def probe_once(client: httpx.AsyncClient, url: str, timeout: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    status, error = None, None
    try:
        resp = await client.get(url, timeout=timeout)
        status = resp.status_code
    except httpx.ConnectError:
        error = "refused"
    except httpx.TimeoutException:
        error = "timeout"
    except httpx.HTTPError:
        error = "error"
    return {"url": url, "status": status, "class": classify(status, error),
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1), "ts": time.time()}


class HttpProber:
    def __init__(self, config: ConfigStore, events: EventStore, docker: DockerWatcher):
        self.config, self.events, self.docker = config, events, docker
        self.results: dict[str, dict[str, Any]] = {}
        self._streak: dict[str, tuple[int, str | None]] = {}

    def handle(self, result: dict[str, Any]) -> None:
        url, cls = result["url"], result["class"]
        self.results[url] = result
        target = self.config.get().target
        if cls == "ok":
            self._streak.pop(url, None)
            return
        if cls == "4xx":
            self._streak.pop(url, None)
            ev, new = self.events.record(
                "suggestion", "http", http_signature(url, "4xx"), url=url,
                http_status=result["status"],
                message=f"{url} answers {result['status']}: missing route or link?")
            if new:
                say("observer", f"suggestion: {url} -> {result['status']} (shown, never patched)")
            return
        st = self.docker.state
        if st.docker_ok and st.state != "running":
            self._streak.pop(url, None)  # Docker side reports this one
            return
        count, run = self._streak.get(url, (0, st.started_at))
        count = count + 1 if run == st.started_at else 1
        self._streak[url] = (count, st.started_at)
        if count >= target.probe_failures_before_crash:
            ev, new = self.events.record(
                "crash", "http", http_signature(url, cls), url=url, http_status=result["status"],
                message=f"{url}: {cls}" + (f" ({result['status']})" if result["status"] else ""),
                status=st.state)
            if new:
                say("observer", f"crash: {url} {cls}")

    async def run(self) -> None:
        # trust_env=False: never route probes through a host proxy.
        async with httpx.AsyncClient(trust_env=False) as client:
            while True:
                target = self.config.get().target
                try:
                    results = await asyncio.gather(*(probe_once(client, p.url, target.probe_timeout_s)
                                                     for p in target.probes))
                    urls = {p.url for p in target.probes}
                    self.results = {u: r for u, r in self.results.items() if u in urls}
                    for r in results:
                        self.handle(r)
                    bus.publish("probes", list(self.results.values()))
                except Exception as exc:  # a probe bug must not stop probing
                    say("observer", f"probe loop error: {exc}")
                await asyncio.sleep(target.probe_interval_s)
