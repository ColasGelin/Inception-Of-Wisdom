"""Wisdom Loop HTTP routes (dashboard tab 3)."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from iow import runtime as rt

router = APIRouter(prefix="/loop")


@router.get("/status")
def status() -> dict:
    return rt.get().loop.status()


@router.get("/heals")
def heals(limit: int = 30) -> dict:
    return {"heals": rt.get().loop.store.list(limit)}


class KillSwitch(BaseModel):
    enabled: bool


@router.post("/kill-switch")
def kill_switch(body: KillSwitch) -> dict:
    """enabled=false refuses every new heal; a heal already running still ends cleanly (healed or rolled back)."""
    loop = rt.get().loop
    loop.set_enabled(body.enabled)
    return {"enabled": loop.guards.enabled}
