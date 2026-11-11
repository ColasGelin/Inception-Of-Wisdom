"""Wires the agent's long-lived parts together.

The same file ships in p1, p2 and p3. A part's features are simply the
packages present in it: p1 has no `iow.analyst`, p2 has no `iow.loop`.
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any

from iow.bus import bus, say
from iow.config import ConfigStore
from iow.observer.docker_watch import DockerWatcher
from iow.observer.events import EventStore
from iow.observer.http_probe import HttpProber


def has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


FEATURES = ["observer"] + (["analyst"] if has("iow.analyst") else []) + (
    ["loop"] if has("iow.loop") else []) + (["bonus"] if has("iow.bonus") else [])


class Runtime:
    def __init__(self) -> None:
        self.config = ConfigStore(log=say)
        cfg = self.config.get
        self.events = EventStore(lambda: (cfg().observer.crash_dedup_window_s,
                                          cfg().observer.suggestion_dedup_window_s))
        self.docker = DockerWatcher(self.config, self.events)
        self.prober = HttpProber(self.config, self.events, self.docker)
        self.analyst: Any = None
        self.loop: Any = None
        self.events.subscribe(lambda ev, new: bus.publish("event", ev.to_dict()))
        if "analyst" in FEATURES:
            from iow.analyst.service import Analyst
            self.analyst = Analyst(self.config)
        if "loop" in FEATURES:
            from iow.loop.wisdom import WisdomLoop
            self.loop = WisdomLoop(self)
        if "bonus" in FEATURES:
            from iow.bonus import attach
            attach(self)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        bus.bind(asyncio.get_running_loop())
        say("agent", f"starting with features: {', '.join(FEATURES)}")
        self.config.start()
        self.docker.start()
        self._tasks.append(asyncio.create_task(self.prober.run()))
        if self.analyst is not None:
            self.analyst.start()
        if self.loop is not None:
            self.loop.start()


runtime: Runtime | None = None


def get() -> Runtime:
    global runtime
    if runtime is None:
        runtime = Runtime()
    return runtime
