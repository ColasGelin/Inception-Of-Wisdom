"""The Wisdom Loop state machine.

IDLE -> (crash event, guards pass) -> SNAPSHOT (original sha, checkout -B iow/auto-heal)
  -> attempt n: DIAGNOSE -> PATCH -> SANITY -> APPLY -> COMMIT -> RESTART -> VERIFY(grace)
     healed     -> HEALED (target keeps running on iow/auto-heal)
     not healed -> n < max ? next attempt with the NEW error log : ROLLBACK
ROLLBACK -> reset --hard <original sha>, checkout original, clean -fd, restart,
            prove the tree equals the original commit blob for blob -> CLEAN FAILURE

Every transition is saved to the history file and pushed over SSE.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from iow.bus import agent_log, bus, say
from iow.loop import git_ops
from iow.loop.guards import Guards
from iow.loop.patch import Patch, apply_patch, failed, propose_patch, sanity_checks
from iow.observer.events import Event, excerpt_of
from iow.observer.http_probe import probe_once
from iow.store import HistoryStore

if TYPE_CHECKING:
    from iow.runtime import Runtime

KILL_SWITCH_FILE = Path(os.environ.get("IOW_KILL_SWITCH", "/data/state/kill_switch.json"))
VERIFY_TICK_S = 0.5  # how often VERIFY looks at the event store; the window itself is grace_period_s
_DIAG_KEYS = ("ok", "stage", "reason", "summary", "files", "candidates", "answer", "llm", "clues",
              "facts", "patch_facts", "first_answer", "reasked")


def _trim_diagnosis(d: dict[str, Any]) -> dict[str, Any]:
    out = {k: d[k] for k in _DIAG_KEYS if k in d}
    if out.get("llm"):
        out["llm"] = {k: v for k, v in out["llm"].items() if k != "raw"}
    out["chunks"] = [{"id": c["id"], "path": c["path"], "start": c["start"], "end": c["end"],
                      "name": c.get("name"), "score": c.get("score"), "why": c.get("why")}
                     for c in d.get("chunks", [])]
    return out


def commit_message(diagnosis: dict[str, Any], attempt: int, signature: str) -> str:
    first = diagnosis["summary"].split(". ")[0].strip().rstrip(".")
    subject = f"iow: {', '.join(diagnosis['files'])}: {first}"
    if len(subject) > 72:
        subject = subject[:69].rstrip() + "..."
    return f"{subject}\n\nattempt {attempt}, crash signature: {signature}\n\n{diagnosis['summary']}"


class WisdomLoop:
    def __init__(self, rt: "Runtime"):
        self.rt = rt
        self.config = rt.config
        self.guards = Guards(rt.config, state_file=KILL_SWITCH_FILE)
        self.store = HistoryStore()
        self.current: dict[str, Any] | None = None
        self.refusals: deque[dict[str, Any]] = deque(maxlen=50)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seen_during_heal: list[str] = []
        rt.events.subscribe(self._on_event)

    @property
    def busy(self) -> bool:
        return self.guards.busy

    @property
    def ws(self) -> Path:
        return Path(self.config.get().target.source_dir)

    # ---- lifecycle ---------------------------------------------------
    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        interrupted = [h for h in self.store.list() if h.get("state") == "interrupted"]
        if interrupted and interrupted[0].get("original"):
            self._loop.create_task(self._recover(interrupted[0]))

    async def _recover(self, heal: dict[str, Any]) -> None:
        """The agent died mid-heal: put the workspace back on the pre-heal commit."""
        snap = git_ops.Snapshot(**heal["original"])
        branch = self.config.get().loop.branch
        say("loop", f"heal {heal['id']} was interrupted by an agent restart: rolling back to {snap.sha[:8]}")
        try:
            await asyncio.to_thread(git_ops.rollback, self.ws, snap, branch)
            check = await asyncio.to_thread(git_ops.verify_tree, self.ws, snap.sha)
            await asyncio.to_thread(self.rt.docker.restart_target)
            heal["rollback"] = {"verify": check, "after_agent_restart": True}
            heal["state"], heal["reason"] = "rolled_back", "agent restarted mid-heal; rolled back on start"
        except Exception as exc:
            heal["state"], heal["reason"] = "failed", f"recovery after restart failed: {exc}"
        heal["ended_at"] = time.time()
        self.store.save(heal)
        bus.publish("heal", heal)

    def set_enabled(self, enabled: bool) -> None:
        self.guards.enabled = enabled
        say("loop", f"kill switch {'released: auto-heal enabled' if enabled else 'ON: every new heal is refused'}")
        bus.publish("loop_status", self.status())

    def status(self) -> dict[str, Any]:
        cfg = self.config.get().loop
        cur = self.current
        return {"guards": self.guards.status(), "config": cfg.model_dump(),
                "current": None if cur is None else {"id": cur["id"], "phase": cur.get("phase"),
                                                     "attempt": len(cur["attempts"]),
                                                     "signature": cur["signature"]},
                "refusals": list(self.refusals)[-15:]}

    # ---- triggering --------------------------------------------------
    def _on_event(self, ev: Event, is_new: bool) -> None:
        if ev.kind != "crash" or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._consider, ev.id)
        except RuntimeError:
            pass

    def _consider(self, event_id: str) -> None:
        ev = self.rt.events.get(event_id)
        if ev is None:
            return
        if self.guards.busy:
            # Recorded, not acted on: the running heal owns the target until it finishes.
            if ev.signature not in self._seen_during_heal:
                self._seen_during_heal.append(ev.signature)
                say("loop", f"crash during the running heal recorded, not acted on: {ev.signature}")
            return
        if self.rt.analyst is None or not self.rt.analyst.ready():
            self._refuse(ev, f"analyst not ready ({getattr(self.rt.analyst, 'state', 'absent')})")
            return
        reason = self.guards.acquire(ev.signature)
        if reason:
            self._refuse(ev, reason)
            return
        asyncio.get_running_loop().create_task(self._run(ev))

    def _refuse(self, ev: Event, reason: str) -> None:
        kind = reason.split(":")[0].split("(")[0].strip()
        last = next((r for r in reversed(self.refusals) if r["signature"] == ev.signature), None)
        if last and last["kind"] == kind:
            last["count"] += 1
            last["last_seen"], last["reason"] = time.time(), reason
        else:
            self.refusals.append({"signature": ev.signature, "kind": kind, "reason": reason,
                                  "count": 1, "first_seen": time.time(), "last_seen": time.time()})
            say("loop", f"heal refused for {ev.signature}: {reason}")
        bus.publish("loop_status", self.status())

    # ---- the heal ----------------------------------------------------
    def _save(self, heal: dict[str, Any]) -> None:
        self.store.save(heal)
        bus.publish("heal", heal)

    def _step(self, heal: dict, attempt: dict | None, name: str, llm: bool = False) -> dict:
        step = {"name": name, "started_at": time.time(), "ended_at": None, "ok": None,
                "detail": "", "llm": llm, "attempt": attempt["n"] if attempt else None}
        (attempt["steps"] if attempt else heal["steps"]).append(step)
        heal["phase"] = f"{name}" + (f" (attempt {attempt['n']})" if attempt else "")
        self._save(heal)
        return step

    def _end(self, heal: dict, step: dict, ok: bool, detail: str = "") -> None:
        step["ended_at"], step["ok"], step["detail"] = time.time(), ok, detail
        self._save(heal)

    async def _run(self, ev: Event) -> None:
        cfg = self.config.get()
        heal: dict[str, Any] = {
            "id": uuid.uuid4().hex[:8], "signature": ev.signature, "event_id": ev.id,
            "trigger": {"kind": ev.kind, "source": ev.source, "message": ev.message,
                        "status": ev.status, "exit_code": ev.exit_code, "log_excerpt": ev.log_excerpt},
            "started_at": time.time(), "ended_at": None, "state": "running", "phase": "snapshot",
            "branch": cfg.loop.branch, "original": None, "attempts": [], "steps": [],
            "rollback": None, "reason": None, "log": [],
            "settings": {"grace_period_s": cfg.loop.grace_period_s, "max_attempts": cfg.loop.max_attempts,
                         "model": cfg.llm.model, "embedding_model": cfg.llm.embedding_model}}
        self.current, self._seen_during_heal = heal, []
        hook = lambda entry: heal["log"].append(entry)  # noqa: E731
        agent_log.hooks.append(hook)
        try:
            say("loop", f"heal {heal['id']} started for crash: {ev.signature}")
            await self._heal(heal, ev)
        except Exception as exc:  # a bug in the loop must not leave the target half-patched
            heal["reason"] = f"loop error: {type(exc).__name__}: {exc}"
            say("loop", heal["reason"])
            if heal.get("original"):
                await self._rollback(heal)
            else:
                heal["state"] = "failed"
        finally:
            heal["ended_at"], heal["phase"] = time.time(), heal["state"]
            agent_log.hooks.remove(hook)
            self.guards.cool_down(self._seen_during_heal + [a.get("crash_signature") for a in heal["attempts"]
                                                           if a.get("crash_signature")])
            self._save(heal)
            self.current = None
            self.guards.release()
            bus.publish("loop_status", self.status())

    async def _heal(self, heal: dict[str, Any], ev: Event) -> None:
        cfg = self.config.get()
        step = self._step(heal, None, "snapshot")
        try:
            snap = await asyncio.to_thread(git_ops.snapshot, self.ws)
            await asyncio.to_thread(git_ops.start_branch, self.ws, cfg.loop.branch, snap.sha)
        except Exception as exc:
            self._end(heal, step, False, str(exc))
            heal["state"], heal["reason"] = "failed", f"snapshot failed: {exc}"
            say("loop", heal["reason"])
            return
        heal["original"] = {"branch": snap.branch, "sha": snap.sha}
        self._end(heal, step, True, f"{snap.branch} @ {snap.sha[:8]} -> checkout -B {cfg.loop.branch}")
        say("git", f"snapshot {snap.branch} @ {snap.sha[:8]}, working on {cfg.loop.branch}")

        excerpt, feedback = ev.log_excerpt or ev.message, ""
        max_attempts = cfg.loop.max_attempts
        for n in range(1, max_attempts + 1):
            attempt: dict[str, Any] = {"n": n, "started_at": time.time(), "ended_at": None, "steps": [],
                                       "outcome": None, "input_excerpt": excerpt, "feedback": feedback}
            heal["attempts"].append(attempt)
            outcome = await self._attempt(heal, attempt, excerpt, feedback)
            attempt["ended_at"] = time.time()
            self._save(heal)
            if outcome == "healed":
                heal["state"] = "healed"
                say("verify", f"HEALED on attempt {n}: target up for "
                              f"{attempt['health']['grace_period_s']:g}s, probes ok, commit "
                              f"{attempt['commit'][:8]} on {cfg.loop.branch}")
                return
            if outcome == "abort":
                break
            excerpt = attempt.get("new_excerpt") or excerpt
            feedback = attempt.get("feedback_next", "")
        heal["reason"] = heal.get("reason") or f"not healed after {len(heal['attempts'])} attempt(s)"
        await self._rollback(heal)

    async def _attempt(self, heal: dict, attempt: dict, excerpt: str, feedback: str) -> str:
        """One DIAGNOSE..VERIFY pass. Returns healed | retry | abort."""
        n, cfg = attempt["n"], self.config.get()
        analyst = self.rt.analyst

        step = self._step(heal, attempt, "diagnose", llm=True)
        diagnosis = await asyncio.to_thread(analyst.diagnose, excerpt, feedback)
        attempt["diagnosis"] = _trim_diagnosis(diagnosis)
        if not diagnosis["ok"]:
            self._end(heal, step, False, f"{diagnosis['stage']}: {diagnosis['reason']}")
            attempt["outcome"] = "diagnosis failed"
            heal["reason"] = f"no usable diagnosis on attempt {n}: {diagnosis['reason']}"
            say("analyst", f"diagnosis failed explicitly: {diagnosis['reason']}; not guessing")
            return "abort"
        top = diagnosis["candidates"][0]
        say("analyst", f"candidates: " + ", ".join(f"{c['path']} ({c['score']:.1f})" for c in diagnosis["candidates"]))
        hits = diagnosis.get("retrieval") or []
        if hits:
            say("analyst", f"chroma top-k {hits[0]['score']:.2f} {hits[0]['path']} ({hits[0]['name']})")
        say("llm", f"\"{', '.join(diagnosis['files'])}\": {diagnosis['summary'][:140]}")
        self._end(heal, step, True, f"suspects {diagnosis['files']} (index top: {top['path']})")

        step = self._step(heal, attempt, "patch", llm=True)
        try:
            patch, stats, _prompt = await asyncio.to_thread(propose_patch, diagnosis, self.ws,
                                                            analyst.llm, feedback)
        except Exception as exc:
            self._end(heal, step, False, str(exc))
            attempt["outcome"] = "no patch"
            if "unreachable" in str(exc) or "timed out" in str(exc):
                heal["reason"] = f"LLM unavailable while patching: {exc}"
                say("llm", heal["reason"])
                return "abort"
            attempt["feedback_next"] = f"your previous answer was unusable ({exc}); return a valid patch"
            say("patch", f"attempt {n}: unusable patch answer: {exc}")
            return "retry"
        attempt["patch"] = patch.model_dump()
        attempt["llm_patch"] = {k: v for k, v in stats.items() if k != "raw"}
        self._end(heal, step, True, f"{len(patch.files)} file(s): " +
                  ", ".join(f"{f.op} {f.path}" for f in patch.files))
        say("llm", f"patch proposed: {patch.summary[:140]}")

        step = self._step(heal, attempt, "sanity")
        checks = sanity_checks(patch, self.ws)
        attempt["checks"] = [c.to_dict() for c in checks]
        bad = failed(checks)
        if bad:
            detail = "; ".join(f"{c.name}: {c.detail}" for c in bad)
            self._end(heal, step, False, detail)
            attempt["outcome"] = "patch rejected by sanity checks"
            attempt["feedback_next"] = f"your previous patch was rejected before writing: {detail}"
            say("patch", f"attempt {n}: rejected before any write: {detail}")
            return "retry"
        self._end(heal, step, True, f"{len(checks)} checks passed")
        say("patch", f"attempt {n}: {len(checks)} sanity checks passed")

        step = self._step(heal, attempt, "apply")
        written = await asyncio.to_thread(apply_patch, Patch(**attempt["patch"]), self.ws)
        self._end(heal, step, True, f"wrote {', '.join(written)} (temp file + os.replace)")

        step = self._step(heal, attempt, "commit")
        message = commit_message(diagnosis, n, heal["signature"])
        sha = await asyncio.to_thread(git_ops.commit_all, self.ws, message)
        attempt["commit"], attempt["commit_message"] = sha, message
        self._end(heal, step, True, f"{sha[:8]} {message.splitlines()[0]}")
        say("git", f"commit on {cfg.loop.branch}: {sha[:8]} {message.splitlines()[0]}")

        health = await self._restart_and_verify(heal, attempt)
        attempt["health"] = health
        if health["healed"]:
            attempt["outcome"] = "healed"
            return "healed"
        attempt["outcome"] = "not healed"
        attempt["crash_signature"] = health.get("signature")
        attempt["new_excerpt"] = health.get("excerpt")
        attempt["feedback_next"] = (f"commit {sha[:8]} did not fix it: {health['reason']}. "
                                    "Look at the new error and try a different fix.")
        say("verify", f"attempt {n} not healed: {health['reason']}")
        return "retry"

    async def _restart(self, heal: dict, attempt: dict | None) -> tuple[float, str | None, bool]:
        step = self._step(heal, attempt, "restart")
        run_before = self.rt.docker.state.started_at
        t0 = time.time()
        try:
            await asyncio.to_thread(self.rt.docker.restart_target)
            self._end(heal, step, True, f"docker restart {self.config.get().target.container}")
            say("docker", f"restarted {self.config.get().target.container}")
            return t0, run_before, True
        except Exception as exc:
            self._end(heal, step, False, str(exc))
            say("docker", f"restart failed: {exc}")
            return t0, run_before, False

    def _crash_after(self, t0: float, run_before: str | None) -> Event | None:
        for ev in self.rt.events.crashes_since(t0):
            if ev.run_id is None or ev.run_id != run_before:
                return ev  # stale reports from the run we just replaced do not count
        return None

    async def _restart_and_verify(self, heal: dict, attempt: dict | None) -> dict[str, Any]:
        t0, run_before, ok = await self._restart(heal, attempt)
        grace = self.config.get().loop.grace_period_s  # read now: a hot edit applies to this verify
        step = self._step(heal, attempt, "verify")
        if not ok:
            result = {"healed": False, "reason": "restart failed", "grace_period_s": grace}
            self._end(heal, step, False, result["reason"])
            return result
        say("verify", f"watching the target for {grace:g}s (grace period)")
        deadline = t0 + grace
        while time.time() < deadline:
            await asyncio.sleep(VERIFY_TICK_S)
            crash = self._crash_after(t0, run_before)
            if crash is not None:
                result = {"healed": False, "grace_period_s": grace,
                          "reason": f"new crash {time.time() - t0:.1f}s after restart: {crash.signature}",
                          "signature": crash.signature, "excerpt": crash.log_excerpt or crash.message,
                          "event_id": crash.id}
                self._end(heal, step, False, result["reason"])
                return result
        result = await self._final_health(t0, run_before, grace)
        self._end(heal, step, result["healed"], result["reason"])
        return result

    async def _final_health(self, t0: float, run_before: str | None, grace: float) -> dict[str, Any]:
        docker = self.rt.docker
        try:
            await asyncio.to_thread(docker.refresh_now)
        except Exception:
            pass  # Docker unreachable: the state stays as last seen and the probes decide
        state = docker.state.state
        target = self.config.get().target
        async with httpx.AsyncClient(trust_env=False) as client:
            probes = list(await asyncio.gather(*(probe_once(client, p.url, target.probe_timeout_s)
                                                 for p in target.probes)))
        bad = [p for p in probes if p["class"] not in ("ok", "4xx")]
        crash = self._crash_after(t0, run_before)
        base = {"grace_period_s": grace, "state": state, "probes": probes}
        if crash is not None:
            return {**base, "healed": False, "reason": f"new crash: {crash.signature}",
                    "signature": crash.signature, "excerpt": crash.log_excerpt or crash.message}
        if state != "running":
            return {**base, "healed": False, "reason": f"container is {state} after the grace period",
                    "excerpt": self._tail_excerpt()}
        if bad:
            return {**base, "healed": False, "excerpt": self._tail_excerpt(),
                    "reason": "probe failed: " + ", ".join(f"{p['url']} {p['class']}" for p in bad)}
        return {**base, "healed": True,
                "reason": f"up for {grace:g}s with no new crash; probes " +
                          ", ".join(f"{p['url'].split(':8000')[-1] or '/'} {p['status']}" for p in probes)}

    def _tail_excerpt(self) -> str:
        docker = self.rt.docker
        lines = docker.logs.run_lines(docker.state.started_at)
        return excerpt_of(lines, self.config.get().observer.log_excerpt_lines)

    async def _rollback(self, heal: dict[str, Any]) -> None:
        cfg = self.config.get()
        snap = git_ops.Snapshot(**heal["original"])
        step = self._step(heal, None, "rollback")
        say("git", f"rolling back {cfg.loop.branch} to {snap.sha[:8]} (git reset --hard + clean)")
        try:
            await asyncio.to_thread(git_ops.rollback, self.ws, snap, cfg.loop.branch)
            check = await asyncio.to_thread(git_ops.verify_tree, self.ws, snap.sha)
            commits = [a["commit"] for a in heal["attempts"] if a.get("commit")]
            check["agent_commits_unreachable"] = await asyncio.to_thread(
                git_ops.unreachable_from_refs, self.ws, commits)
        except Exception as exc:
            self._end(heal, step, False, str(exc))
            heal["state"], heal["reason"] = "failed", f"{heal.get('reason')}; rollback failed: {exc}"
            say("git", f"ROLLBACK FAILED: {exc}")
            return
        self._end(heal, step, check["ok"], "tree == original commit, blob for blob" if check["ok"]
                  else f"tree differs: {check}")
        say("git", f"tree verified against {snap.sha[:8]}: head {'ok' if check['head_matches'] else 'DIFFERS'}, "
                   f"status {'clean' if check['status_clean'] else 'DIRTY'}, diff {'empty' if check['diff_empty'] else 'NOT EMPTY'}, "
                   f"{'all blobs match' if check['blobs_match'] else 'blobs differ'}")
        health = await self._restart_and_verify(heal, None)
        heal["rollback"] = {"verify": check, "health": health, "original": heal["original"]}
        heal["state"] = "rolled_back" if check["ok"] else "failed"
        say("verify", f"CLEAN FAILURE: rolled back to {snap.sha[:8]}, target restarted on the original code "
                      f"({'still crashing, as before the heal' if not health['healed'] else 'running'})")
