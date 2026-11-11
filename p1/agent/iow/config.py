"""iow.config.yml: schema, defaults and hot reload.

Every timing value the agent uses lives here so a peer can change it without
touching code. A bad file never takes the agent down: the last good config
stays active and the error is shown in the dashboard.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Callable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_PATH = Path(os.environ.get("IOW_CONFIG", "/pristine/iow.config.yml"))


class _Strict(BaseModel):
    # Unknown keys are typos ("grace_periode_s") that would silently keep a
    # default; reject them so the dashboard shows the mistake.
    model_config = ConfigDict(extra="forbid")


class Probe(_Strict):
    url: str


class TargetCfg(_Strict):
    container: str = "iow-target"
    source_dir: str = "/workspace/target"
    probes: list[Probe] = Field(default_factory=lambda: [
        Probe(url="http://iow-target:8000/"), Probe(url="http://iow-target:8000/health")])
    probe_interval_s: float = Field(3, gt=0)
    probe_timeout_s: float = Field(2, gt=0)
    probe_failures_before_crash: int = Field(2, ge=1)


class RestartLoopCfg(_Strict):
    restarts: int = Field(3, ge=2)
    window_s: float = Field(60, gt=0)


class ObserverCfg(_Strict):
    error_patterns: list[str] = Field(default_factory=lambda: [
        "Traceback", "Error:", "Exception", "exited with code"])
    crash_dedup_window_s: float = Field(30, gt=0)
    suggestion_dedup_window_s: float = Field(300, gt=0)
    restart_loop: RestartLoopCfg = Field(default_factory=RestartLoopCfg)
    log_settle_s: float = Field(1.0, ge=0)
    log_excerpt_lines: int = Field(60, ge=5)
    reconnect_backoff_max_s: float = Field(10, gt=0)


class LlmCfg(_Strict):
    host: str = "http://ollama:11434"
    model: str = "qwen2.5-coder:1.5b"
    embedding_model: str = "nomic-embed-text"
    timeout_s: float = Field(120, gt=0)
    seed: int = 42
    num_ctx: int = Field(8192, ge=2048)


class AnalystCfg(_Strict):
    top_k: int = Field(5, ge=1, le=50)
    max_candidates: int = Field(4, ge=1, le=10)
    reindex_debounce_s: float = Field(1.0, ge=0)
    watch_poll_s: float = Field(1.0, gt=0)
    exclude: list[str] = Field(default_factory=lambda: [
        ".git", "__pycache__", "iow.config.yml", ".pytest_cache"])


class LoopCfg(_Strict):
    enabled: bool = True
    max_attempts: int = Field(3, ge=1, le=3)
    grace_period_s: float = Field(20, gt=0)
    cooldown_per_signature_s: float = Field(600, ge=0)
    max_heals_per_hour: int = Field(6, ge=0)
    max_heals_lifetime: int = Field(30, ge=0)
    branch: str = Field("iow/auto-heal", pattern=r"^[A-Za-z0-9._/-]+$")
    restart_timeout_s: float = Field(10, gt=0)


class Config(_Strict):
    target: TargetCfg = Field(default_factory=TargetCfg)
    observer: ObserverCfg = Field(default_factory=ObserverCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    analyst: AnalystCfg = Field(default_factory=AnalystCfg)
    loop: LoopCfg = Field(default_factory=LoopCfg)
    reload_interval_s: float = Field(1.0, gt=0)


def parse(text: str) -> Config:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ValueError("top level of iow.config.yml must be a mapping")
    return Config.model_validate(data)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
    return f"{type(exc).__name__}: {exc}".splitlines()[0]


class ConfigStore:
    """Holds the active config; a background thread polls the file for changes.

    Polling (not inotify) because bind mounts from Docker Desktop hosts do not
    reliably deliver inotify events into the container.
    """

    def __init__(self, path: Path = CONFIG_PATH, log: Callable[[str, str], None] | None = None):
        self.path = path
        self._log = log or (lambda c, m: None)
        self._config = Config()
        self.error: str | None = None
        self.loaded_at: float | None = None
        self._digest: str | None = None
        self._listeners: list[Callable[[Config, Config], None]] = []
        self._lock = threading.Lock()
        self.reload()

    def get(self) -> Config:
        return self._config

    def on_change(self, fn: Callable[[Config, Config], None]) -> None:
        self._listeners.append(fn)

    def reload(self) -> bool:
        """Re-read the file if its content changed. Returns True if a new config was applied."""
        with self._lock:
            try:
                raw = self.path.read_bytes()
            except OSError as exc:
                if self.error is None or "not readable" not in self.error:
                    self.error = f"{self.path} not readable ({exc.strerror}); using last good config"
                    self._log("config", self.error)
                return False
            digest = hashlib.sha256(raw).hexdigest()
            if digest == self._digest:
                return False
            self._digest = digest
            try:
                new = parse(raw.decode("utf-8"))
            except Exception as exc:  # yaml, pydantic or decode errors: keep the last good one
                self.error = _short_error(exc)
                self._log("config", f"invalid iow.config.yml, keeping last good config: {self.error}")
                return False
            old, self._config = self._config, new
            self.error = None
            first = self.loaded_at is None
            self.loaded_at = time.time()
        self._log("config", "loaded iow.config.yml" if first else "iow.config.yml changed, new values active")
        for fn in self._listeners:
            fn(old, new)
        return True

    def watch_forever(self) -> None:
        while True:
            time.sleep(self._config.reload_interval_s)
            try:
                self.reload()
            except Exception as exc:  # never let the watcher thread die
                self._log("config", f"reload failed: {exc}")

    def start(self) -> None:
        threading.Thread(target=self.watch_forever, name="config-watch", daemon=True).start()
