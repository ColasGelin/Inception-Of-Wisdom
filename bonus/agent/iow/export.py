"""`make export-run`: the latest healed run and the latest rolled-back run as JSON lines.

One line per record, in order: a header, every step (with its timestamp, attempt,
whether it used the LLM, its checks and the agent log lines printed while it
ran), then the final state. Container paths are kept relative; anything that
looks like a host home directory is masked.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from iow.store import HistoryStore

_HOST = re.compile(r"(/home/[^/\s\"']+|/Users/[^/\s\"']+|[A-Za-z]:\\Users\\[^\\\s\"']+)")


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return _HOST.sub("<host>", value.replace("/workspace/target/", ""))
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    return value


def _assign_lines(log: list[dict], starts: list[float]) -> list[list[str]]:
    """Each log line goes to exactly one step: the last one started at or before it."""
    out: list[list[str]] = [[] for _ in starts]
    for e in log:
        idx = max((i for i, t in enumerate(starts) if t <= e["ts"] + 0.001), default=0)
        out[idx].append(e["text"])
    return out


def records(heal: dict[str, Any]) -> list[dict[str, Any]]:
    log = heal.get("log", [])
    out: list[dict[str, Any]] = [{
        "type": "run", "run_id": heal["id"], "ts": heal["started_at"], "trigger": {
            k: heal["trigger"].get(k) for k in ("source", "message", "status", "exit_code", "log_excerpt")},
        "signature": heal["signature"], "branch": heal["branch"], "settings": heal.get("settings"),
        "original_commit": (heal.get("original") or {}).get("sha")}]
    steps = list(heal.get("steps", []))
    for a in heal.get("attempts", []):
        steps += a["steps"]
    steps.sort(key=lambda s: s["started_at"])
    attempts = {a["n"]: a for a in heal.get("attempts", [])}
    lines = _assign_lines(log, [st["started_at"] for st in steps]) if steps else []
    for seq, s in enumerate(steps, 1):
        rec: dict[str, Any] = {"type": "step", "seq": seq, "ts": s["started_at"], "ended_ts": s["ended_at"],
                               "attempt": s["attempt"], "step": s["name"], "used_llm": s["llm"],
                               "ok": s["ok"], "detail": s["detail"], "log": lines[seq - 1]}
        a = attempts.get(s["attempt"]) or {}
        if s["name"] == "diagnose" and a.get("diagnosis"):
            d = a["diagnosis"]
            rec["diagnosis"] = {k: d.get(k) for k in ("ok", "summary", "files", "reason")}
            rec["candidates"] = [{"path": c["path"], "score": round(c["score"], 2), "why": c["reasons"]}
                                 for c in d.get("candidates", [])]
            rec["chunks"] = [{"id": c["id"], "score": c["score"]} for c in d.get("chunks", [])]
        if s["name"] == "patch" and a.get("patch"):
            rec["patch"] = a["patch"]
        if s["name"] == "sanity" and a.get("checks"):
            rec["checks"] = [{"check": c["name"], "passed": c["passed"], "detail": c["detail"]} for c in a["checks"]]
        if s["name"] == "commit" and a.get("commit"):
            rec["commit"] = a["commit"][:12]
        if s["name"] == "verify" and s["attempt"] and a.get("health"):
            rec["checks"] = [{"check": f"probe {p['url']}", "passed": p["class"] in ("ok", "4xx"),
                              "detail": f"{p['status']} {p['class']}"} for p in a["health"].get("probes", [])]
        if s["name"] == "rollback" and heal.get("rollback"):
            v = heal["rollback"]["verify"]
            rec["checks"] = [{"check": k, "passed": bool(v.get(k))} for k in
                             ("head_matches", "status_clean", "diff_empty", "blobs_match", "agent_commits_unreachable")
                             if k in v]
        out.append(rec)
    out.append({"type": "final", "run_id": heal["id"], "ts": heal["ended_at"], "state": heal["state"],
                "attempts": len(heal.get("attempts", [])), "reason": heal.get("reason"),
                "duration_s": round((heal["ended_at"] or heal["started_at"]) - heal["started_at"], 1)})
    return [_clean(r) for r in out]


def main(out_dir: Path) -> None:
    store = HistoryStore()
    out_dir.mkdir(parents=True, exist_ok=True)
    wrote = 0
    for state, name in (("healed", "healed-run.jsonl"), ("rolled_back", "rollback-run.jsonl")):
        heal = store.latest(state)
        if heal is None:
            print(f"[export] no {state} run in the history yet")
            continue
        path = out_dir / name
        with path.open("w", encoding="utf-8") as fh:
            for rec in records(heal):
                fh.write(json.dumps(rec, default=str) + "\n")
        wrote += 1
        print(f"[export] {state} run {heal['id']} ({heal['signature']}) -> exports/{name}")
    if not wrote:
        sys.exit(1)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/exports"))
