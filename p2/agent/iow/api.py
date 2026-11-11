"""HTTP surface of the agent: JSON routes, the SSE stream, and the dashboard."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from iow import runtime as rt
from iow import workspace
from iow.bus import agent_log, bus, say
from iow.gitcmd import GitError

WEB = Path(__file__).resolve().parent.parent / "web"
SSE_HEARTBEAT_S = 15  # keeps proxies from closing an idle stream; not a behaviour knob


@asynccontextmanager
async def lifespan(app: FastAPI):
    await rt.get().start()
    yield


app = FastAPI(title="Inception-of-Wisdom agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/status")
def status() -> dict:
    r = rt.get()
    cfg = r.config.get()
    data = {
        "features": rt.FEATURES,
        "target": r.docker.state.to_dict(),
        "probes": [r.prober.results.get(p.url, {"url": p.url, "class": "pending"})
                   for p in cfg.target.probes],
        "config": cfg.model_dump(),
        "config_error": r.config.error,
        "config_loaded_at": r.config.loaded_at,
    }
    if r.analyst is not None:
        data["analyst"] = r.analyst.status()
    if r.loop is not None:
        data["loop"] = r.loop.status()
    return data


@app.get("/logs")
def logs(n: int = 300) -> dict:
    r = rt.get()
    return {"lines": r.docker.logs.tail(max(1, min(n, 1000))), "agent": list(agent_log.lines)[-n:]}


@app.get("/events")
async def events_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events: log, agent_log, event, status, probes (+ index, heal)."""
    queue = bus.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=SSE_HEARTBEAT_S)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/events/recent")
def events_recent(limit: int = 100) -> dict:
    return {"events": [e.to_dict() for e in rt.get().events.list(limit)]}


@app.get("/config")
def config() -> dict:
    r = rt.get()
    return {"config": r.config.get().model_dump(), "error": r.config.error}


@app.get("/demo/bugs")
def bugs() -> dict:
    return {"bugs": workspace.available_bugs()}


@app.post("/demo/bug/{name}")
def plant_bug(name: str) -> dict:
    """Demo helper behind `make bug`: commit a planted bug on main, restart the target."""
    r = rt.get()
    if r.loop is not None and r.loop.busy:
        raise HTTPException(409, "a heal is in progress; plant the bug when it has finished")
    try:
        sha = workspace.plant(name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GitError as exc:
        raise HTTPException(409, str(exc)) from exc
    say("demo", f"plant bug: {name} committed on main @ {sha[:8]}, restarting target")
    try:
        r.docker.restart_target()
    except Exception as exc:
        raise HTTPException(503, f"bug committed but restart failed: {exc}") from exc
    return {"bug": name, "commit": sha}


if "analyst" in rt.FEATURES:
    from iow.analyst.routes import router as analyst_router
    app.include_router(analyst_router)
if "loop" in rt.FEATURES:
    from iow.loop.routes import router as loop_router
    app.include_router(loop_router)
if "bonus" in rt.FEATURES:
    from iow.bonus.routes import router as bonus_router
    app.include_router(bonus_router)
