import pytest

from iow.loop.patch import (FileChange, Patch, apply_patch, failed, normalise, parse_file_answer,
                            propose_patch, sanity_checks, strip_fences)

ORIGINAL = ('"""Routes."""\n\nfrom fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n'
            '@router.get("/health")\ndef health() -> dict[str, str]:\n    return {"status": "ok"}\n')


@pytest.fixture
def root(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text(ORIGINAL)
    (tmp_path / "iow.config.yml").write_text("loop: {}\n")
    return tmp_path


def _patch(*files, summary="fix the health route"):
    return Patch(summary=summary, files=[FileChange(path=p, op=op, content=c) for p, op, c in files])


def _failed_names(patch, root):
    return [c.name.split(":")[0] for c in failed(sanity_checks(patch, root))]


GOOD = ORIGINAL.replace("ok", "healthy")


def test_a_good_patch_passes_every_check(root):
    assert failed(sanity_checks(_patch(("app/routes.py", "modify", GOOD)), root)) == []


def test_rejects_more_than_three_files(root):
    files = [(f"app/new{i}.py", "create", "x = 1\n" * 10) for i in range(4)]
    assert "at most 3 files" in _failed_names(_patch(*files), root)


@pytest.mark.parametrize("content", ["pass\n", "...\n", "# TODO\n", "x = 1\n",
                                     ORIGINAL.replace('    return {"status": "ok"}', "    # rest of the file unchanged"),
                                     ORIGINAL + "\n# ... existing code ...\n",
                                     ORIGINAL.replace('    return {"status": "ok"}', "    ...")])
def test_rejects_placeholders(root, content):
    assert "no placeholder" in _failed_names(_patch(("app/routes.py", "modify", content)), root)


def test_rejects_shrinking_more_than_60_percent(root):
    small = 'from fastapi import APIRouter\nrouter = APIRouter()\n'  # real code, but 75 % smaller
    names = _failed_names(_patch(("app/routes.py", "modify", small)), root)
    assert "shrink <= 60%" in names and "no placeholder" not in names


def test_allows_shrinking_less_than_60_percent(root):
    smaller = ORIGINAL.replace('"""Routes."""\n\n', "")
    assert "shrink <= 60%" not in _failed_names(_patch(("app/routes.py", "modify", smaller)), root)


@pytest.mark.parametrize("leak", ["<<<CHUNK app/routes.py:1-9>>>\n", "<<<END CHUNK>>>\n",
                                  "<<<FILE app/routes.py>>>\n", "FACTS FROM THE CODE INDEX:\n",
                                  "   1| import os\n   2| import sys\n"])
def test_rejects_retrieval_markers(root, leak):
    names = _failed_names(_patch(("app/routes.py", "modify", leak + GOOD)), root)
    assert "no retrieval markers" in names


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "app/../../x.py", ".git/config",
                                  "iow.config.yml", "C:/x.py"])
def test_rejects_unsafe_paths(root, path):
    assert "safe path" in _failed_names(_patch((path, "modify", GOOD)), root)


def test_rejects_symlink_escape(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (root / "link").symlink_to(outside)
    assert "safe path" in _failed_names(_patch(("link/x.py", "create", GOOD)), root)


def test_rejects_wrong_ops_and_noops(root):
    assert "op" in _failed_names(_patch(("app/missing.py", "modify", GOOD)), root)
    assert "op" in _failed_names(_patch(("app/routes.py", "create", GOOD)), root)
    assert "changes something" in _failed_names(_patch(("app/routes.py", "modify", ORIGINAL)), root)


def test_rejects_python_that_does_not_parse(root):
    broken = GOOD.replace("def health() -> dict[str, str]:", "def health() -> dict[str, str]")
    assert "python parses" in _failed_names(_patch(("app/routes.py", "modify", broken)), root)


def test_strip_fences_and_normalise():
    assert strip_fences("```python\nx = 1\n```") == "x = 1\n"
    p = normalise(_patch(("./app/routes.py", "modify", "```\nx = 1\n```")))
    assert p.files[0].path == "app/routes.py" and p.files[0].content == "x = 1\n"


def test_apply_is_atomic_per_file_and_handles_delete(root):
    (root / "app" / "old.py").write_text("x = 1\n")
    apply_patch(_patch(("app/routes.py", "modify", GOOD), ("app/old.py", "delete", ""),
                       ("app/new.py", "create", "y = 2\n")), root)
    assert (root / "app" / "routes.py").read_text() == GOOD
    assert not (root / "app" / "old.py").exists() and (root / "app" / "new.py").read_text() == "y = 2\n"
    assert not list(root.rglob("*.iow-tmp"))


def test_parse_file_answer_accepts_annotated_fence():
    s, content = parse_file_answer("SUMMARY: fixed it\n\n```python  # app/main.py\nx = 1\n```\n")
    assert s == "fixed it" and content == "x = 1\n"
    with pytest.raises(ValueError):
        parse_file_answer("no code here")


def test_propose_patch_builds_the_json_from_plain_text(root):
    from fakes import FakeLlm
    llm = FakeLlm(f"SUMMARY: renamed the status\n```python\n{GOOD}```\n")
    diagnosis = {"summary": "health returns the wrong word", "files": ["app/routes.py"],
                 "clues": {"condensed": "", "target_frames": []}, "candidates": []}
    patch, _stats, prompt = propose_patch(diagnosis, root, llm)
    assert patch.files[0].model_dump() == {"path": "app/routes.py", "op": "modify", "content": GOOD}
    assert ORIGINAL.strip() in prompt  # the full current file was given to the model


def test_propose_patch_rejects_unchanged_files(root):
    from fakes import FakeLlm
    llm = FakeLlm(f"SUMMARY: nothing\n```python\n{ORIGINAL}```\n")
    diagnosis = {"summary": "x", "files": ["app/routes.py"], "clues": {}, "candidates": []}
    with pytest.raises(ValueError):
        propose_patch(diagnosis, root, llm)
