"""Analyst HTTP routes (dashboard tab 2)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from iow import runtime as rt
from iow.bus import say
from iow.llm import LlmError

router = APIRouter(prefix="/analyst")


@router.get("/status")
def status() -> dict:
    return rt.get().analyst.status()


@router.get("/search")
async def search(q: str, k: int | None = None) -> dict:
    try:
        hits = await asyncio.to_thread(rt.get().analyst.search, q, k)
    except LlmError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"query": q, "results": hits}


class DiagnoseRequest(BaseModel):
    event_id: str | None = None
    log_excerpt: str | None = None


@router.post("/diagnose")
async def diagnose(req: DiagnoseRequest) -> dict:
    r = rt.get()
    excerpt = req.log_excerpt
    if req.event_id:
        ev = r.events.get(req.event_id)
        if ev is None:
            raise HTTPException(404, "unknown event id (events live in memory; refresh the list)")
        if ev.kind != "crash":
            raise HTTPException(400, "only crash events are diagnosed; suggestions are never patched")
        excerpt = ev.log_excerpt or ev.message
    if not excerpt:
        raise HTTPException(400, "give an event_id or a log_excerpt")
    say("analyst", "diagnosis requested from the dashboard")
    result = await asyncio.to_thread(r.analyst.diagnose, excerpt)
    if result["ok"]:
        say("llm", f"{', '.join(result['files'])}: {result['summary'][:120]}")
    else:
        say("analyst", f"diagnosis failed explicitly: {result['reason']}")
    return result
