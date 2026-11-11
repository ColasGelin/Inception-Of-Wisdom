import pytest

from iow.gitcmd import branch, git, head
from iow.loop import git_ops


def test_snapshot_refuses_a_dirty_tree(git_repo):
    (git_repo / "app" / "main.py").write_text("dirty\n")
    with pytest.raises(git_ops.GitError):
        git_ops.snapshot(git_repo)


def test_heal_branch_commit_then_rollback_is_byte_for_byte(git_repo):
    snap = git_ops.snapshot(git_repo)
    original = {p: p.read_bytes() for p in git_repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    git_ops.start_branch(git_repo, "iow/auto-heal", snap.sha)
    commits = []
    for i in range(3):
        (git_repo / "app" / "main.py").write_text(f"attempt {i}\n")
        (git_repo / "app" / f"new{i}.py").write_text("x = 1\n")
        commits.append(git_ops.commit_all(git_repo, f"iow: attempt {i}"))
    (git_repo / "untracked.txt").write_text("left behind\n")
    git_ops.rollback(git_repo, snap, "iow/auto-heal")
    result = git_ops.verify_tree(git_repo, snap.sha)
    assert result["ok"] and result["blobs_match"] and result["status_clean"] and result["diff_empty"]
    assert branch(git_repo) == "main" and head(git_repo) == snap.sha
    assert git(git_repo, "rev-parse", "iow/auto-heal") == snap.sha
    assert git_ops.unreachable_from_refs(git_repo, commits)
    now = {p: p.read_bytes() for p in git_repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert now == original


def test_verify_tree_detects_a_changed_byte(git_repo):
    sha = head(git_repo)
    path = git_repo / "app" / "text.py"
    path.write_bytes(path.read_bytes().replace(b"Hello", b"Hellp"))
    result = git_ops.verify_tree(git_repo, sha)
    assert not result["ok"] and result["mismatched"] == ["app/text.py"]


def test_rollback_when_the_heal_started_on_the_heal_branch(git_repo):
    git_ops.start_branch(git_repo, "iow/auto-heal", head(git_repo))
    snap = git_ops.snapshot(git_repo)  # e.g. a second crash after an earlier successful heal
    (git_repo / "app" / "main.py").write_text("bad\n")
    sha = git_ops.commit_all(git_repo, "iow: bad")
    git_ops.rollback(git_repo, snap, "iow/auto-heal")
    assert git_ops.verify_tree(git_repo, snap.sha)["ok"] and git_ops.unreachable_from_refs(git_repo, [sha])
