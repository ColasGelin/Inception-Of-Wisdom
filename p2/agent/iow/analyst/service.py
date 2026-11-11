"""The Analyst: prepares models, keeps the index in sync, answers search and diagnosis."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from iow.analyst.diagnose import diagnose
from iow.analyst.index import CodeIndex
from iow.analyst.watcher import DebouncedWatcher
from iow.bus import bus, say
from iow.config import Config, ConfigStore
from iow.llm import LlmError, OllamaClient

CHROMA_PATH = Path(os.environ.get("IOW_CHROMA", "/data/chroma"))


class Analyst:
    def __init__(self, config: ConfigStore, llm: OllamaClient | None = None):
        self.config = config
        self.llm = llm or OllamaClient(config)
        self.index: CodeIndex | None = None
        self.state = "starting"   # starting | pulling | indexing | ready | error
        self.error: str | None = None
        self.model_info: dict[str, Any] = {}
        self._watcher: DebouncedWatcher | None = None
        self._sync_lock = threading.Lock()
        self._last_sync_error: str | None = None
        config.on_change(self._on_config_change)

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._boot, name="analyst-boot", daemon=True).start()

    def _set(self, state: str, error: str | None = None) -> None:
        self.state, self.error = state, error
        bus.publish("index", self.status())

    def _boot(self) -> None:
        backoff = 2.0
        while True:
            try:
                self._prepare_models()
                self._open_index()
                break
            except Exception as exc:  # Ollama not up yet, network, disk...
                self._set("error", f"{type(exc).__name__}: {exc}"[:300])
                say("analyst", f"not ready: {self.error}; retrying in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, self.config.get().observer.reconnect_backoff_max_s * 3)
        if self._watcher is None:
            cfg = self.config.get()
            self._watcher = DebouncedWatcher(
                cfg.target.source_dir, self.reindex,
                debounce=lambda: self.config.get().analyst.reindex_debounce_s,
                poll=cfg.analyst.watch_poll_s, on_error=self._on_sync_error)
            self._watcher.start()
            say("analyst", f"watching {cfg.target.source_dir} for changes")

    def _prepare_models(self) -> None:
        cfg = self.config.get().llm
        for model in (cfg.embedding_model, cfg.model):
            if not self.llm.has(model):
                self._set("pulling")
                say("llm", f"model {model} not present, pulling it (first start only)")
                self.llm.pull(model, lambda m: say("llm", m))
        info = self.llm.show(cfg.model)
        if not info["under_3b"]:
            raise LlmError(f"{cfg.model} has {info['params_human']} parameters: the subject requires "
                           "strictly fewer than 3B, refusing to use it")
        self.model_info = info
        say("llm", f"chat model {cfg.model}: {info['params_human']} parameters "
                   f"({info['parameter_count']:,}), under 3B")

    def _open_index(self) -> None:
        cfg = self.config.get()
        self._set("indexing")
        self.index = CodeIndex(CHROMA_PATH, Path(cfg.target.source_dir), self.llm.embed,
                               cfg.llm.embedding_model, cfg.analyst.exclude,
                               log=lambda m: say("analyst", m))
        self.reindex(first=True)
        self._set("ready")

    def reindex(self, first: bool = False) -> dict | None:
        if self.index is None:
            return None
        with self._sync_lock:
            stats = self.index.sync()
        self._last_sync_error = None
        n = len(stats["changed"]) + len(stats["removed"])
        if first and n == 0:
            say("analyst", f"0 files changed, index reused ({stats['files']} files, "
                           f"{self.index.stats()['chunks']} chunks, nothing re-embedded)")
        elif n:
            for p in stats["changed"]:
                say("analyst", f"re-embedded {p}")
            for p in stats["removed"]:
                say("analyst", f"removed {p} from the index")
            say("analyst", f"{n} file(s) changed, {stats['chunks_embedded']} chunks embedded "
                           f"in {stats['elapsed_s']}s")
        bus.publish("index", self.status())
        return stats

    def _on_sync_error(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"[:200]
        if msg != self._last_sync_error:
            say("analyst", f"reindex failed ({msg}); will retry")
        self._last_sync_error = msg

    def _on_config_change(self, old: Config, new: Config) -> None:
        if (old.llm.model, old.llm.embedding_model, old.llm.host) != (
                new.llm.model, new.llm.embedding_model, new.llm.host) and self.state != "starting":
            say("analyst", "llm settings changed: preparing models again")
            threading.Thread(target=self._reload_models, daemon=True).start()

    def _reload_models(self) -> None:
        try:
            self._prepare_models()
            if self.index is not None and self.index.embedding_model != self.config.get().llm.embedding_model:
                self._open_index()
            self._set("ready")
        except Exception as exc:
            self._set("error", f"{type(exc).__name__}: {exc}"[:300])
            say("analyst", f"model change failed: {self.error}")

    # ---- queries -----------------------------------------------------
    def ready(self) -> bool:
        return self.state == "ready" and self.index is not None

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "error": self.error, "model": self.model_info,
                "index": self.index.stats() if self.index else None,
                "reindex_error": self._last_sync_error}

    def search(self, query: str, k: int | None = None) -> list[dict]:
        if not self.ready():
            raise LlmError(f"analyst not ready ({self.state})")
        return self.index.search(query, k or self.config.get().analyst.top_k)

    def diagnose(self, excerpt: str, feedback: str = "") -> dict[str, Any]:
        if not self.ready():
            return {"ok": False, "stage": "analyst", "reason": f"analyst not ready ({self.state}: {self.error})"}
        cfg = self.config.get().analyst
        return diagnose(excerpt, self.index, self.llm, cfg.top_k, cfg.max_candidates, feedback)
