"""Hard bounds that keep the loop from running away.

single-flight · per-signature cooldown · hourly rate · lifetime cap · kill switch.
Every number comes from iow.config.yml at the moment of the check, so a hot
config edit applies to the very next decision.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from iow.config import ConfigStore

HOUR_S = 3600.0  # the definition of "per hour", not a tunable


class Guards:
    def __init__(self, config: ConfigStore, clock: Callable[[], float] = time.time,
                 state_file: Path | None = None):
        self.config, self.clock = config, clock
        self._state_file = state_file
        # An operator who flipped the switch off must not see heals resume after an agent restart.
        self._enabled = self._load_enabled(config.get().loop.enabled)
        self.busy = False
        self._lock = threading.Lock()
        self._last_heal: dict[str, float] = {}   # signature -> time of the last heal start
        self._starts: deque[float] = deque()
        self.lifetime = 0
        config.on_change(self._on_config)

    def _load_enabled(self, default: bool) -> bool:
        try:
            return bool(json.loads(self._state_file.read_text())["enabled"])  # type: ignore[union-attr]
        except (AttributeError, OSError, ValueError, KeyError, TypeError):
            return default

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value
        if self._state_file is not None:
            try:
                self._state_file.write_text(json.dumps({"enabled": value, "at": time.time()}))
            except OSError:
                pass  # the in-memory switch still applies

    def _on_config(self, old, new) -> None:
        if old.loop.enabled != new.loop.enabled:  # the file wins only when its value changes
            self.enabled = new.loop.enabled

    def _refusal(self, signature: str, now: float) -> str | None:
        loop = self.config.get().loop
        if not self.enabled:
            return "kill switch is off: every new heal is refused"
        if self.busy:
            return "a heal is already running (single-flight)"
        last = self._last_heal.get(signature)
        if last is not None and now - last < loop.cooldown_per_signature_s:
            return (f"cooldown for this signature: {loop.cooldown_per_signature_s - (now - last):.0f}s "
                    f"left of {loop.cooldown_per_signature_s:g}s")
        while self._starts and now - self._starts[0] > HOUR_S:
            self._starts.popleft()
        if len(self._starts) >= loop.max_heals_per_hour:
            return f"hourly rate limit reached ({len(self._starts)}/{loop.max_heals_per_hour} in the last hour)"
        if self.lifetime >= loop.max_heals_lifetime:
            return f"lifetime cap reached ({self.lifetime}/{loop.max_heals_lifetime}); restart the agent to reset"
        return None

    def acquire(self, signature: str) -> str | None:
        """Atomically check every bound and take the single-flight slot. None = go."""
        with self._lock:
            now = self.clock()
            reason = self._refusal(signature, now)
            if reason is None:
                self.busy = True
                self._last_heal[signature] = now
                self._starts.append(now)
                self.lifetime += 1
            return reason

    def cool_down(self, signatures: list[str]) -> None:
        """Signatures produced by the heal's own attempts get a cooldown too."""
        with self._lock:
            now = self.clock()
            for sig in signatures:
                self._last_heal[sig] = max(self._last_heal.get(sig, 0.0), now)

    def release(self) -> None:
        with self._lock:
            self.busy = False

    def status(self) -> dict:
        loop = self.config.get().loop
        now = self.clock()
        with self._lock:
            hour = sum(1 for t in self._starts if now - t <= HOUR_S)
            cooldowns = {s: round(loop.cooldown_per_signature_s - (now - t))
                         for s, t in self._last_heal.items() if now - t < loop.cooldown_per_signature_s}
        return {"enabled": self.enabled, "busy": self.busy, "heals_last_hour": hour,
                "max_heals_per_hour": loop.max_heals_per_hour, "lifetime": self.lifetime,
                "max_heals_lifetime": loop.max_heals_lifetime, "cooldowns": cooldowns}
