import pytest

from iow import workspace
from iow.gitcmd import GitError, git, is_clean


@pytest.fixture
def layout(tmp_path):
    pristine = tmp_path / "pristine"
    (pristine / "app").mkdir(parents=True)
    (pristine / "app" / "main.py").write_text("print('ok')\n")
    (pristine / "iow.config.yml").write_text("loop: {}\n")
    bugs = tmp_path / "bugs"
    (bugs / "syntax" / "app").mkdir(parents=True)
    (bugs / "syntax" / "app" / "main.py").write_text("print('ok'\n")
    (bugs / "none").mkdir()
    return tmp_path / "ws", pristine, bugs


def test_init_creates_repo_without_config_and_is_idempotent(layout):
    ws, pristine, _ = layout
    sha = workspace.init(ws, pristine)
    assert (ws / "app" / "main.py").exists() and not (ws / "iow.config.yml").exists()
    (ws / "app" / "main.py").write_text("changed\n")
    git(ws, "commit", "-qam", "change")
    assert workspace.init(ws, pristine) != sha  # kept, not rebuilt
    assert workspace.init(ws, pristine, force=True) and (ws / "app" / "main.py").read_text() == "print('ok')\n"


def test_plant_commits_one_bug_on_main(layout):
    ws, pristine, bugs = layout
    workspace.init(ws, pristine)
    workspace.plant("syntax", ws, pristine, bugs)
    assert git(ws, "log", "-1", "--format=%s") == "plant bug: syntax"
    assert (ws / "app" / "main.py").read_text() == "print('ok'\n"
    workspace.plant("none", ws, pristine, bugs)
    assert (ws / "app" / "main.py").read_text() == "print('ok')\n" and is_clean(ws)


def test_plant_refuses_unknown_bug_and_dirty_tree(layout):
    ws, pristine, bugs = layout
    workspace.init(ws, pristine)
    with pytest.raises(ValueError):
        workspace.plant("nope", ws, pristine, bugs)
    (ws / "app" / "main.py").write_text("dirty\n")
    with pytest.raises(GitError):
        workspace.plant("syntax", ws, pristine, bugs)
