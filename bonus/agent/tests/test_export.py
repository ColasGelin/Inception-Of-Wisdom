import json

from iow import export
from iow.store import HistoryStore


def _heal(state, **extra):
    t = 100.0
    steps = [{"name": "snapshot", "started_at": t, "ended_at": t + 1, "ok": True, "detail": "", "llm": False, "attempt": None}]
    a_steps = [{"name": n, "started_at": t + i + 2, "ended_at": t + i + 2.5, "ok": True, "detail": "/workspace/target/app/main.py",
                "llm": n in ("diagnose", "patch"), "attempt": 1} for i, n in enumerate(["diagnose", "patch", "sanity", "verify"])]
    return {"id": state[:4], "signature": "E", "branch": "iow/auto-heal", "state": state, "started_at": t,
            "ended_at": t + 10, "trigger": {"source": "docker", "message": "m", "status": "exited (1)", "exit_code": 1,
                                             "log_excerpt": "File \"/home/alice/x.py\""},
            "original": {"branch": "main", "sha": "a" * 40}, "steps": steps,
            "attempts": [{"n": 1, "steps": a_steps, "checks": [{"name": "c", "passed": True, "detail": ""}],
                          "diagnosis": {"ok": True, "summary": "s", "files": ["app/main.py"], "candidates": [], "chunks": []},
                          "patch": {"summary": "p", "files": []}, "health": {"probes": []}}],
            "log": [{"ts": t + 0.5, "text": "[loop] start"}, {"ts": t + 2.1, "text": "[llm] diag"},
                    {"ts": t + 2.9, "text": "[analyst] after diagnose ended"}, {"ts": t + 5.2, "text": "[verify] HEALED"}],
            **extra}


def test_records_are_ordered_with_each_log_line_once_and_no_host_paths():
    recs = export.records(_heal("healed"))
    steps = [r for r in recs if r["type"] == "step"]
    assert [r["step"] for r in steps] == ["snapshot", "diagnose", "patch", "sanity", "verify"]
    assert [r["used_llm"] for r in steps] == [False, True, True, False, False]
    all_lines = [line for r in steps for line in r["log"]]
    assert sorted(all_lines) == sorted(e["text"] for e in _heal("healed")["log"])  # each exactly once
    assert "[analyst] after diagnose ended" in steps[1]["log"]
    dumped = json.dumps(recs)
    assert "/home/alice" not in dumped and "/workspace/target/" not in dumped
    assert recs[-1] == {**recs[-1], "type": "final", "state": "healed", "attempts": 1}


def test_main_writes_both_runs(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "h.jsonl")
    store.save(_heal("healed"))
    store.save({**_heal("rolled_back"), "id": "rb01", "started_at": 200.0})
    monkeypatch.setattr(export, "HistoryStore", lambda: HistoryStore(tmp_path / "h.jsonl"))
    export.main(tmp_path / "out")
    assert (tmp_path / "out" / "healed-run.jsonl").exists() and (tmp_path / "out" / "rollback-run.jsonl").exists()
