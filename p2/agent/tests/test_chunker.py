from iow.analyst.chunker import chunk_file

PY = '''"""Doc."""
import os

from app.text import greet

LIMIT = 3


@router.get("/")
def index():
    return greet("x")


class Thing:
    def a(self):
        return 1

    def b(self):
        return 2
'''


def test_python_chunks_are_logical_units():
    chunks = chunk_file("app/routes.py", PY)
    assert [(c.kind, c.name) for c in chunks] == [
        ("module", "module header"), ("function", "index"), ("class", "Thing")]
    header, fn, cls = chunks
    assert "import os" in header.text and "LIMIT = 3" in header.text
    assert fn.start == 9 and fn.text.startswith('@router.get("/")')  # decorator included
    assert cls.text.rstrip().endswith("return 2") and cls.end == 19


def test_line_ranges_match_the_file():
    lines = PY.splitlines()
    for c in chunk_file("app/routes.py", PY):
        assert c.text == "\n".join(lines[c.start - 1:c.end])


def test_broken_python_still_chunks_by_definition():
    broken = PY.replace("def index():", "def index()")
    chunks = chunk_file("app/routes.py", broken)
    names = [c.name for c in chunks]
    assert "index" in names and "Thing" in names
    assert any("def index()" in c.text for c in chunks)


def test_big_class_is_split_into_methods():
    body = "\n".join(f"    def m{i}(self):\n        return {i}\n" for i in range(50))
    chunks = chunk_file("big.py", f"class Big:\n{body}")
    assert chunks[0].kind == "class" and chunks[1].kind == "method" and chunks[1].name == "Big.m0"


def test_yaml_by_top_level_key():
    text = "target:\n  a: 1\nobserver:\n  b: 2\n  c: 3\n"
    chunks = chunk_file("conf.yml", text)
    assert [(c.name, c.start, c.end) for c in chunks] == [("target", 1, 2), ("observer", 3, 5)]


def test_dockerfile_by_instruction_group():
    text = "FROM python:3.12\nARG X\n\nRUN pip install x\n\nCMD [\"run\"]\n"
    assert [c.name for c in chunk_file("Dockerfile", text)] == ["FROM", "RUN", "CMD"]


def test_empty_file_has_no_chunk():
    assert chunk_file("app/__init__.py", "") == []
