"""Heal history: a JSON-lines file in the agent's state volume.

Each line is a full snapshot of one heal at one transition. Reloading keeps
the last snapshot per heal id, so the dashboard history survives restarts and
the file doubles as an audit log of every transition with its timestamp.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

HISTORY = Path(os.environ.get("IOW_HISTORY", "/data/state/history.jsonl"))


class HistoryStore:
    def __init__(self, path: Path = HISTORY):
        self.path = path
        self._lock = threading.Lock()
        self.heals: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line after a crash
            if "id" in rec:
                self.heals[rec["id"]] = rec
        for h in self.heals.values():
            if h.get("state") == "running":  # the agent died mid-heal
                h["state"] = "interrupted"

    def save(self, heal: dict[str, Any]) -> None:
        with self._lock:
            self.heals[heal["id"]] = heal
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(heal, default=str) + "\n")

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(self.heals.values(), key=lambda h: h["started_at"], reverse=True)[:limit]

    def latest(self, state: str) -> dict[str, Any] | None:
        return next((h for h in self.list(10_000) if h.get("state") == state), None)
