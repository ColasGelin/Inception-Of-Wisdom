"""Bonus HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter

from iow.bonus import gitea

router = APIRouter(prefix="/bonus")


@router.get("/gitea")
def gitea_info() -> dict:
    return {"repo_url": f"{gitea.PUBLIC}/{gitea.OWNER}/{gitea.REPO}",
            "bootstrapped": gitea.TOKEN_FILE.exists()}
