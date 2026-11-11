"""The target's runtime workspace: a git repository built from the pristine source.

The submission repo cannot contain a nested .git, so the pristine target is
copied into `.runtime/target/` and turned into a repository on first start.
`python -m iow.workspace init [--force]` runs in the one-shot `init` service.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from iow.gitcmd import GitError, branch, git, head, is_clean

WORKSPACE = Path(os.environ.get("IOW_WORKSPACE", "/workspace/target"))
PRISTINE = Path(os.environ.get("IOW_PRISTINE", "/pristine"))
BUGS = Path(os.environ.get("IOW_BUGS", "/bugs"))
# Never copied into the workspace: the config is the operator's, not the target's code.
SKIP = {".git", "iow.config.yml", "__pycache__", ".runtime", ".pytest_cache"}


def _clear(ws: Path) -> None:
    for child in ws.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()


def _copy_tree(src: Path, dst: Path) -> None:
    for item in src.iterdir():
        if item.name in SKIP:
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(exist_ok=True)
            _copy_tree(item, target)
        elif item.name != ".keep":
            shutil.copy2(item, target)


def _chown(path: Path, uid: int, gid: int) -> None:
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            os.lchown(os.path.join(root, n), uid, gid)


def init(ws: Path, pristine: Path, force: bool = False) -> str:
    ws.mkdir(parents=True, exist_ok=True)
    if (ws / ".git").exists() and not force:
        sha = head(ws)
        print(f"[init] workspace exists at {branch(ws)} {sha[:8]}, kept as is", flush=True)
        return sha
    if (ws / ".git").exists():
        shutil.rmtree(ws / ".git")
    _clear(ws)
    _copy_tree(pristine, ws)
    git(ws, "init", "-q", "-b", "main")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "initial: pristine target")
    sha = head(ws)
    print(f"[init] workspace created from pristine source, main @ {sha[:8]}", flush=True)
    return sha


def available_bugs(bugs: Path = BUGS) -> list[str]:
    return sorted(p.name for p in bugs.iterdir() if p.is_dir()) if bugs.is_dir() else []


def plant(name: str, ws: Path = WORKSPACE, pristine: Path = PRISTINE, bugs: Path = BUGS) -> str:
    """Pristine code + the bug overlay, committed on main. One bug at a time."""
    if name not in available_bugs(bugs):
        raise ValueError(f"unknown bug {name!r}; choose one of {', '.join(available_bugs(bugs))}")
    if not is_clean(ws):
        raise GitError("workspace has uncommitted changes; run `make reset` or commit them first")
    git(ws, "checkout", "-q", "main")
    _clear(ws)
    _copy_tree(pristine, ws)
    _copy_tree(bugs / name, ws)
    git(ws, "add", "-A")
    if is_clean(ws):
        return head(ws)
    git(ws, "commit", "-q", "-m", f"plant bug: {name}")
    return head(ws)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "init":
        sys.exit("usage: python -m iow.workspace init [--force]")
    runtime = Path(os.environ.get("IOW_RUNTIME", "/runtime"))
    workspace = runtime / "target"
    init(workspace, PRISTINE, force="--force" in sys.argv)
    if os.geteuid() == 0:
        # Hand everything to the host user so the agent and the target (same UID) own it.
        _chown(runtime, int(os.environ.get("IOW_UID", "1000")), int(os.environ.get("IOW_GID", "1000")))
