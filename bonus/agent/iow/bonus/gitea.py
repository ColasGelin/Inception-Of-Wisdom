"""After a heal: push the heal branch to the local Gitea and open (or update) a pull request.

The loop never merges: a human reviews the PR. The Gitea instance is a mirror of the runtime
workspace, which stays the source of truth.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from iow.bus import say
from iow.config import ConfigStore
from iow.gitcmd import GitError, git

TOKEN_FILE = Path(os.environ.get("IOW_GITEA_TOKEN_FILE", "/bootstrap/token"))
GITEA = os.environ.get("IOW_GITEA_URL", "http://gitea:3000")
PUBLIC = os.environ.get("IOW_GITEA_PUBLIC_URL", "http://127.0.0.1:3042")
OWNER, REPO = "iow", "target"


def pr_body(heal: dict[str, Any]) -> str:
    ok = [a for a in heal["attempts"] if a.get("outcome") == "healed"]
    a = ok[-1] if ok else heal["attempts"][-1]
    d = a.get("diagnosis", {})
    diagnosis = {"summary": d.get("summary"), "files": d.get("files")}
    tries = "\n".join(f"- attempt {x['n']}: {x.get('outcome')}" + (f" (`{x['commit'][:8]}`)" if x.get("commit") else "")
                      for x in heal["attempts"])
    return (f"Opened by the IoW agent after an automatic heal. **Not merged automatically: please review.**\n\n"
            f"**Crash signature:** `{heal['signature']}`\n\n"
            f"**Verification:** {a.get('health', {}).get('reason', '')}\n\n"
            f"### Attempts\n{tries}\n\n"
            f"### Diagnosis\n```json\n{json.dumps(diagnosis, indent=2)}\n```\n\n"
            f"### Patch\n```json\n{json.dumps(a.get('patch'), indent=2)}\n```\n")


class GiteaPublisher:
    def __init__(self, config: ConfigStore):
        self.config = config

    def _token(self) -> str:
        try:
            return TOKEN_FILE.read_text().strip()
        except OSError as exc:
            raise RuntimeError("Gitea is not bootstrapped yet (no token)") from exc

    def _push(self, ws: Path, token: str, refspec: str, force: bool = False) -> None:
        # Token in a header, never in the URL, so it cannot leak into error messages or git config.
        git(ws, "-c", f"http.extraHeader=Authorization: token {token}", "push", "-q",
            *(["--force"] if force else []), f"{GITEA}/{OWNER}/{REPO}.git", refspec)

    def _recreate_repo(self, api: httpx.Client) -> None:
        api.delete(f"/repos/{OWNER}/{REPO}")
        r = api.post("/user/repos", json={"name": REPO, "default_branch": "main",
                                          "description": "Mirror of the IoW target workspace"})
        r.raise_for_status()

    def publish_sync(self, heal: dict[str, Any]) -> dict[str, Any]:
        token = self._token()
        ws = Path(self.config.get().target.source_dir)
        branch = heal["branch"]
        base = heal["original"]["branch"] if heal["original"]["branch"] not in ("HEAD", branch) else "main"
        with httpx.Client(base_url=f"{GITEA}/api/v1", trust_env=False, timeout=30,
                          headers={"Authorization": f"token {token}"}) as api:
            try:
                self._push(ws, token, f"{base}:{base}")
            except GitError:
                # `make reset` rewrote the workspace history: the mirror is rebuilt, never force-pushed over.
                say("gitea", f"remote {base} diverged from the workspace: recreating the mirror repository")
                self._recreate_repo(api)
                self._push(ws, token, f"{base}:{base}")
            self._push(ws, token, f"{branch}:{branch}", force=True)  # the agent's own branch, rewritten per heal
            title = f"iow: heal {heal['signature']}"[:120]
            body = pr_body(heal)
            for tries_left in (2, 1, 0):
                # Right after the first push into an empty repository Gitea can still answer
                # the pulls API with an error for a moment (observed): retry briefly.
                try:
                    r = api.get(f"/repos/{OWNER}/{REPO}/pulls", params={"state": "open"})
                    r.raise_for_status()
                    existing = next((p for p in r.json() if p["head"]["ref"] == branch), None)
                    if existing:
                        r = api.patch(f"/repos/{OWNER}/{REPO}/pulls/{existing['number']}",
                                      json={"title": title, "body": body})
                    else:
                        r = api.post(f"/repos/{OWNER}/{REPO}/pulls",
                                     json={"head": branch, "base": base, "title": title, "body": body})
                    r.raise_for_status()
                    pr = r.json()
                    break
                except httpx.HTTPStatusError:
                    if not tries_left:
                        raise
                    time.sleep(2)
        url = f"{PUBLIC}/{OWNER}/{REPO}/pulls/{pr['number']}"
        return {"number": pr["number"], "url": url, "updated": bool(existing), "base": base, "head": branch}

    async def publish(self, heal: dict[str, Any]) -> None:
        import asyncio
        try:
            heal["pr"] = await asyncio.to_thread(self.publish_sync, heal)
            say("gitea", f"pull request #{heal['pr']['number']} {'updated' if heal['pr']['updated'] else 'opened'}: "
                         f"{heal['pr']['url']} (review it, the agent never merges)")
        except Exception as exc:  # a forge problem must never undo or fail a good heal
            heal["pr_error"] = f"{type(exc).__name__}: {exc}"[:300]
            say("gitea", f"could not open the pull request: {heal['pr_error']}")
