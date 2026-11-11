"""Plain `git` subprocess calls: easy to read, easy to replay by hand."""

from __future__ import annotations

import subprocess
from pathlib import Path

IDENTITY = ("-c", "user.name=IoW Agent", "-c", "user.email=iow@localhost",
            # The workspace may be owned by another UID than the caller (init runs as root).
            "-c", "safe.directory=*", "-c", "commit.gpgsign=false")


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *IDENTITY, "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def is_clean(repo: Path) -> bool:
    return git(repo, "status", "--porcelain") == ""
