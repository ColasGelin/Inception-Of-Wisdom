"""Git side of the loop: snapshot, branch, commit, rewind, prove the rewind.

Undo is never an inverse patch: the original commit hash is recorded before
the first write, and rollback is `reset --hard` to it plus `clean -fd`, then a
blob-by-blob comparison of the working tree against that commit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from iow.gitcmd import IDENTITY, GitError, branch, git, head, is_clean


@dataclass
class Snapshot:
    branch: str  # "HEAD" when detached
    sha: str


def snapshot(ws: Path) -> Snapshot:
    if not is_clean(ws):
        raise GitError("the workspace has uncommitted changes; refusing to heal on top of them "
                       "(run `make reset`, or commit them)")
    return Snapshot(branch(ws), head(ws))


def start_branch(ws: Path, name: str, sha: str) -> None:
    """(Re)create the heal branch at the snapshot: earlier heals never leak into this one."""
    git(ws, "checkout", "-q", "-B", name, sha)


def commit_all(ws: Path, message: str) -> str:
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", message)
    return head(ws)


def rollback(ws: Path, snap: Snapshot, heal_branch: str) -> None:
    git(ws, "reset", "-q", "--hard", snap.sha)        # heal branch tip back to the original
    if snap.branch not in ("HEAD", heal_branch):
        git(ws, "checkout", "-q", "-f", snap.branch)
        git(ws, "reset", "-q", "--hard", snap.sha)
    elif snap.branch == "HEAD":
        git(ws, "checkout", "-q", "-f", "--detach", snap.sha)
    git(ws, "clean", "-q", "-fd")
    if snap.branch != heal_branch:
        # Agent commits become unreachable from every ref (branch points at the original).
        git(ws, "branch", "-q", "-f", heal_branch, snap.sha)


def _hash_object(ws: Path, rel: str) -> str:
    proc = subprocess.run(["git", *IDENTITY, "-C", str(ws), "hash-object", "--", rel],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return "<missing>"
    return proc.stdout.strip()


def verify_tree(ws: Path, sha: str) -> dict:
    """Byte-for-byte: every file of commit `sha` hashes to the same blob on disk, and nothing else exists."""
    mismatched = []
    for line in git(ws, "ls-tree", "-r", sha).splitlines():
        meta, rel = line.split("\t", 1)
        _mode, kind, blob = meta.split()
        if kind == "blob" and _hash_object(ws, rel) != blob:
            mismatched.append(rel)
    status = git(ws, "status", "--porcelain")
    diff_empty = subprocess.run(["git", *IDENTITY, "-C", str(ws), "diff", "--quiet", sha],
                                timeout=30).returncode == 0
    result = {"head": head(ws), "expected": sha, "head_matches": head(ws) == sha,
              "status_clean": status == "", "diff_empty": diff_empty,
              "blobs_match": not mismatched, "mismatched": mismatched}
    result["ok"] = all((result["head_matches"], result["status_clean"], diff_empty, not mismatched))
    return result


def unreachable_from_refs(ws: Path, shas: list[str]) -> bool:
    """True when none of `shas` is reachable from any branch or tag."""
    for sha in shas:
        if git(ws, "branch", "-a", "--contains", sha, check=False) or \
                git(ws, "tag", "--contains", sha, check=False):
            return False
    return True


__all__ = ["GitError", "Snapshot", "snapshot", "start_branch", "commit_all", "rollback",
           "verify_tree", "unreachable_from_refs"]
