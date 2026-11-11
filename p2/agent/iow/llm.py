"""Client for the local Ollama runtime: structured chat, embeddings, model info.

Only ever talks to the Ollama container on the compose network (or a host
Ollama the operator points `llm.host` at). There is no remote model anywhere.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import httpx

from iow.config import ConfigStore

MAX_PARAMS = 3_000_000_000  # subject rule: strictly fewer than 3 billion parameters


class LlmError(RuntimeError):
    pass


def full_name(model: str) -> str:
    return model if ":" in model else f"{model}:latest"


def human_params(n: int | None) -> str:
    if not n:
        return "?"
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.0f}M"


class OllamaClient:
    def __init__(self, config: ConfigStore):
        self.config = config

    @property
    def _cfg(self):
        return self.config.get().llm

    def _client(self, timeout: float | None = None) -> httpx.Client:
        # trust_env=False: never route model traffic through a host proxy.
        return httpx.Client(base_url=self._cfg.host, timeout=timeout or self._cfg.timeout_s,
                            trust_env=False)

    # ---- models ------------------------------------------------------
    def tags(self) -> list[str]:
        with self._client(10) as c:
            r = c.get("/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]

    def has(self, model: str) -> bool:
        return full_name(model) in {full_name(t) for t in self.tags()}

    def pull(self, model: str, progress: Callable[[str], None]) -> None:
        last_pct = -10
        with self._client(None) as c, c.stream("POST", "/api/pull",
                                               json={"model": model, "stream": True},
                                               timeout=None) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                msg = json.loads(line)
                if "error" in msg:
                    raise LlmError(f"pull {model}: {msg['error']}")
                total, done = msg.get("total"), msg.get("completed")
                if total and done:
                    pct = int(done * 100 / total)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        progress(f"pulling {model}: {pct}% of {total / 1e6:.0f} MB")

    def show(self, model: str) -> dict[str, Any]:
        with self._client(30) as c:
            r = c.post("/api/show", json={"model": model})
            r.raise_for_status()
            data = r.json()
        info = data.get("model_info") or {}
        details = data.get("details") or {}
        params = info.get("general.parameter_count")
        return {"model": model, "parameter_count": params,
                "parameter_size": details.get("parameter_size"), "params_human": human_params(params),
                "quantization": details.get("quantization_level"), "family": details.get("family"),
                "under_3b": bool(params) and params < MAX_PARAMS}

    # ---- inference ---------------------------------------------------
    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """One chat turn constrained to `schema`. Returns (parsed JSON, call stats)."""
        cfg = self._cfg
        body = {"model": cfg.model, "stream": False, "format": schema, "keep_alive": "30m",
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "options": {"temperature": 0, "seed": cfg.seed, "num_ctx": cfg.num_ctx}}
        t0 = time.time()
        try:
            with self._client() as c:
                r = c.post("/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
        except httpx.TimeoutException as exc:
            raise LlmError(f"LLM timed out after {cfg.timeout_s:g}s") from exc
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM unreachable at {cfg.host}: {exc}") from exc
        content = (data.get("message") or {}).get("content", "")
        if not data.get("done", True):
            raise LlmError(f"LLM stopped before finishing its answer ({len(content)} chars, done=false)")
        stats = {"model": cfg.model, "elapsed_s": round(time.time() - t0, 2),
                 "prompt_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count"),
                 "raw": content[:4000]}
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM answer is not valid JSON: {content[:200]!r}") from exc
        if not isinstance(parsed, dict):
            raise LlmError("LLM answer is not a JSON object")
        return parsed, stats

    def chat_text(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        """One plain chat turn (temperature 0, fixed seed). Used for file contents: a 1.5B model
        does not reliably escape code inside a JSON string (see DECISIONS.md)."""
        cfg = self._cfg
        body = {"model": cfg.model, "stream": False, "keep_alive": "30m",
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "options": {"temperature": 0, "seed": cfg.seed, "num_ctx": cfg.num_ctx}}
        t0 = time.time()
        try:
            with self._client() as c:
                r = c.post("/api/chat", json=body)
                r.raise_for_status()
                data = r.json()
        except httpx.TimeoutException as exc:
            raise LlmError(f"LLM timed out after {cfg.timeout_s:g}s") from exc
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM unreachable at {cfg.host}: {exc}") from exc
        content = (data.get("message") or {}).get("content", "")
        return content, {"model": cfg.model, "elapsed_s": round(time.time() - t0, 2),
                         "prompt_tokens": data.get("prompt_eval_count"),
                         "output_tokens": data.get("eval_count"), "raw": content[:4000]}

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            with self._client() as c:
                r = c.post("/api/embed", json={"model": self._cfg.embedding_model, "input": texts,
                                               "keep_alive": "30m"})
                r.raise_for_status()
                vectors = r.json().get("embeddings") or []
        except httpx.HTTPError as exc:
            raise LlmError(f"embedding model unreachable at {self._cfg.host}: {exc}") from exc
        if len(vectors) != len(texts):
            raise LlmError(f"embedding returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors
