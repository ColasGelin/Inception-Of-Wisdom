"""The whole loop on a temporary git repository, with a fake Docker target and a fake LLM."""

import asyncio
from datetime import datetime, timezone

import pytest
from fakes import FakeLlm

from iow.gitcmd import branch, git, head
from iow.llm import LlmError
from iow.loop import wisdom
from iow.loop.git_ops import unreachable_from_refs, verify_tree
from iow.observer.docker_watch import LogBuffer, TargetState
from iow.observer.events import EventStore
from iow.store import HistoryStore

TRACE = ('Traceback (most recent call last):\n  File "/app/app/main.py", line 3, in <module>\n'
         "    BUG\nNameError: name 'BUG' is not defined")
BROKEN = "from fastapi import FastAPI\n\nBUG\n\napp = FastAPI()\n\n\n@app.get('/')\ndef index() -> dict:\n    return {'message': 'hi'}\n"
FIXED = BROKEN.replace("BUG\n\n", "")


class FakeTarget:
    """Restarting it 'runs' app/main.py: a crash event if the file still contains BUG."""

    def __init__(self, ws, events):
        self.ws, self.events = ws, events
        self.state = TargetState(container="iow-target", state="restarting",
                                 started_at="2026-01-01T00:00:00.000000Z", docker_ok=True)
        self.logs = LogBuffer()
        self.restarts = 0
        self.stale_kill = False
        self.actual_run = self.state.started_at  # what Docker knows; the polled state can lag

    def restart_target(self):
        self.restarts += 1
        if self.stale_kill:  # Docker reports the replaced run's death (137) after the restart began
            self.events.record("crash", "docker", "exit:137", run_id=self.actual_run, exit_code=137)
        run = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.state.started_at = self.actual_run = run
        if "BUG" in (self.ws / "app" / "main.py").read_text():
            self.state.state = "restarting"
            self.events.record("crash", "docker", "NameError: name 'BUG' is not defined", run_id=run,
                               log_excerpt=TRACE, exit_code=1)
        else:
            self.state.state = "running"

    def refresh_now(self):
        self.state.started_at = self.actual_run

    @property
    def healthy(self):
        return self.state.state == "running"


class FakeAnalyst:
    def __init__(self, llm, diagnosis=None):
        self.llm = llm
        self.calls = []
        self.diagnosis = diagnosis

    def ready(self):
        return True

    def diagnose(self, excerpt, feedback=""):
        self.calls.append((excerpt, feedback))
        if self.diagnosis is not None:
            return self.diagnosis
        return {"ok": True, "summary": "main.py references an undefined name BUG on line 3.",
                "files": ["app/main.py"], "clues": {"condensed": TRACE, "target_frames": []},
                "candidates": [{"path": "app/main.py", "score": 5.0, "reasons": ["traceback: line 3"]}],
                "retrieval": [], "chunks": []}


class FakeRuntime:
    def __init__(self, cfg, ws, llm, diagnosis=None):
        self.config = cfg
        self.events = EventStore(lambda: (30.0, 300.0))
        self.docker = FakeTarget(ws, self.events)
        self.analyst = FakeAnalyst(llm, diagnosis)


@pytest.fixture
def setup(tmp_path, git_repo, config_file, monkeypatch):
    (git_repo / "app" / "main.py").write_text(BROKEN)
    git(git_repo, "commit", "-qam", "plant bug")
    cfg = config_file(f"target: {{ source_dir: {git_repo} }}\n"
                      "loop: { grace_period_s: 0.3, cooldown_per_signature_s: 600 }\n")
    monkeypatch.setattr(wisdom, "HistoryStore", lambda: HistoryStore(tmp_path / "history.jsonl"))
    monkeypatch.setattr(wisdom, "KILL_SWITCH_FILE", tmp_path / "kill.json")
    monkeypatch.setattr(wisdom, "VERIFY_TICK_S", 0.05)

    async def probe(client, url, timeout):
        ok = rt_holder[0].docker.healthy
        return {"url": url, "status": 200 if ok else None, "class": "ok" if ok else "refused",
                "latency_ms": 1, "ts": 0}
    monkeypatch.setattr(wisdom, "probe_once", probe)
    rt_holder = []

    def make(*answers, diagnosis=None):
        rt = FakeRuntime(cfg, git_repo, FakeLlm(*answers), diagnosis)
        rt_holder.append(rt)
        return rt
    return make, git_repo


def run_heal(rt):
    async def go():
        loop = wisdom.WisdomLoop(rt)
        loop.start()
        ev, _ = rt.events.record("crash", "docker", "NameError: name 'BUG' is not defined",
                                 run_id="r0", log_excerpt=TRACE, exit_code=1)
        await asyncio.sleep(0.05)  # _on_event -> _consider -> heal task
        for _ in range(400):
            if not loop.busy:
                break
            await asyncio.sleep(0.05)
        return loop, loop.store.list()[0] if loop.store.list() else None
    return asyncio.run(go())


def answer(content, summary="removed the stray name"):
    return f"SUMMARY: {summary}\n```python\n{content}```\n"


def test_heals_on_the_first_attempt(setup):
    make, ws = setup
    original = head(ws)
    rt = make(answer(FIXED))
    loop, heal = run_heal(rt)
    assert heal["state"] == "healed" and len(heal["attempts"]) == 1
    a = heal["attempts"][0]
    assert [s["name"] for s in a["steps"]] == ["diagnose", "patch", "sanity", "apply", "commit", "restart", "verify"]
    assert branch(ws) == "iow/auto-heal" and git(ws, "rev-parse", "HEAD~1") == original
    assert git(ws, "log", "-1", "--format=%s").startswith("iow: app/main.py:")
    assert git(ws, "rev-parse", "main") == original  # main untouched
    assert (ws / "app" / "main.py").read_text() == FIXED and rt.docker.healthy
    assert heal["log"] and any("HEALED on attempt 1" in e["text"] for e in heal["log"])


def test_three_failures_roll_back_byte_for_byte(setup):
    make, ws = setup
    original_sha = head(ws)
    original = {p: p.read_bytes() for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts}
    bad = [answer(BROKEN.replace("'hi'", f"'hi {i}'")) for i in range(3)]
    rt = make(*bad)
    loop, heal = run_heal(rt)
    assert heal["state"] == "rolled_back" and len(heal["attempts"]) == 3
    commits = [a["commit"] for a in heal["attempts"]]
    assert all(commits) and unreachable_from_refs(ws, commits)
    assert verify_tree(ws, original_sha)["ok"] and heal["rollback"]["verify"]["ok"]
    assert branch(ws) == "main" and head(ws) == original_sha
    assert {p: p.read_bytes() for p in ws.rglob("*") if p.is_file() and ".git" not in p.parts} == original
    assert rt.docker.restarts == 4  # one per attempt + the restart on the original code
    # each retry got the new error log and a note about the failed commit
    assert "did not fix it" in rt.analyst.calls[1][1]


def test_sanity_rejection_writes_nothing_and_retries(setup):
    make, ws = setup
    before = head(ws)
    placeholder = BROKEN.replace("    return {'message': 'hi'}", "    # rest of the file unchanged")
    rt = make(answer(placeholder), answer(FIXED))
    loop, heal = run_heal(rt)
    first, second = heal["attempts"]
    assert first["outcome"] == "patch rejected by sanity checks" and "commit" not in first
    assert any(not c["passed"] and c["name"].startswith("no placeholder") for c in first["checks"])
    assert second["outcome"] == "healed" and git(ws, "rev-parse", "HEAD~1") == before


def test_unusable_diagnosis_stops_without_guessing(setup):
    make, ws = setup
    sha = head(ws)
    rt = make(answer(FIXED), diagnosis={"ok": False, "stage": "validate", "reason": "no suspect file"})
    loop, heal = run_heal(rt)
    assert heal["state"] == "rolled_back" and len(heal["attempts"]) == 1
    assert "no usable diagnosis" in heal["reason"] and head(ws) == sha and branch(ws) == "main"
    assert rt.analyst.llm.prompts == []  # the patch model was never asked


def test_llm_down_while_patching_rolls_back(setup):
    make, ws = setup
    rt = make(LlmError("LLM unreachable at http://ollama:11434: connection refused"))
    loop, heal = run_heal(rt)
    assert heal["state"] == "rolled_back" and len(heal["attempts"]) == 1
    assert "LLM unavailable" in heal["reason"]


def test_kill_switch_refuses_new_heals(setup):
    make, ws = setup
    rt = make(answer(FIXED))

    async def go():
        loop = wisdom.WisdomLoop(rt)
        loop.start()
        loop.set_enabled(False)
        rt.events.record("crash", "docker", "NameError: name 'BUG' is not defined", run_id="r0", log_excerpt=TRACE)
        await asyncio.sleep(0.1)
        return loop
    loop = asyncio.run(go())
    assert loop.store.list() == [] and "kill switch" in loop.refusals[0]["reason"]


def test_events_during_a_heal_are_not_acted_on_and_cooldown_holds(setup):
    make, ws = setup
    rt = make(*[answer(BROKEN.replace("'hi'", f"'hi {i}'")) for i in range(3)])
    loop, heal = run_heal(rt)
    assert len(loop.store.list()) == 1  # crashes from its own restarts did not start new heals

    async def again():
        loop.start()
        rt.events.record("crash", "docker", "NameError: name 'BUG' is not defined", run_id="later",
                         log_excerpt=TRACE, now=10**10)
        await asyncio.sleep(0.1)
    asyncio.run(again())
    assert len(loop.store.list()) == 1 and "cooldown" in loop.refusals[-1]["reason"]


def test_grace_period_is_read_from_config_at_verify_time(setup):
    make, ws = setup
    rt = make(answer(FIXED))
    rt.config.path.write_text(rt.config.path.read_text().replace("grace_period_s: 0.3", "grace_period_s: 0.1"))
    assert rt.config.reload()
    loop, heal = run_heal(rt)
    assert heal["attempts"][0]["health"]["grace_period_s"] == 0.1


def test_history_survives_a_restart(setup, tmp_path):
    make, ws = setup
    rt = make(answer(FIXED))
    run_heal(rt)
    reloaded = HistoryStore(tmp_path / "history.jsonl")
    assert reloaded.latest("healed")["attempts"][0]["outcome"] == "healed"


def test_the_replaced_runs_own_death_does_not_fail_verify(setup):
    make, ws = setup
    rt = make(answer(FIXED))
    rt.docker.stale_kill = True
    rt.docker.actual_run = "2026-01-01T00:00:05.000000Z"  # crash loop moved on since the last poll
    rt.docker.refresh_now = lambda: None  # and the refresh does not help: only the start-time rule does
    loop, heal = run_heal(rt)
    assert heal["state"] == "healed" and len(heal["attempts"]) == 1
