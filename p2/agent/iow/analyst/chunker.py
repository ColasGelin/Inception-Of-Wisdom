"""Logical chunks: one per function/class/module header, not fixed-size windows.

A chunk is what a human would point at ("the health() route", "the imports
of main.py"), so retrieval hits read naturally and the model sees whole units.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass

BIG_CLASS_LINES = 120  # above this a class is split into its header and methods


@dataclass
class Chunk:
    path: str
    start: int  # 1-based, inclusive
    end: int
    kind: str   # module | function | class | method | section | block
    name: str
    text: str

    @property
    def id(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"

    def to_dict(self) -> dict:
        return {**asdict(self), "id": self.id}


def _slice(lines: list[str], start: int, end: int) -> str:
    return "\n".join(lines[start - 1:end])


def _node_start(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", [])
    return min([d.lineno for d in decorators] + [node.lineno])


def python_chunks(path: str, text: str) -> list[Chunk]:
    tree = ast.parse(text)
    lines = text.splitlines()
    chunks: list[Chunk] = []
    header: list[ast.stmt] = []

    def flush_header() -> None:
        if header:
            start, end = _node_start(header[0]), header[-1].end_lineno or header[-1].lineno
            name = "module header" if not chunks else "module statements"
            chunks.append(Chunk(path, start, end, "module", name, _slice(lines, start, end)))
            header.clear()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            flush_header()
            start, end = _node_start(node), node.end_lineno or node.lineno
            if isinstance(node, ast.ClassDef) and end - start > BIG_CLASS_LINES:
                methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                first = _node_start(methods[0]) - 1 if methods else end
                chunks.append(Chunk(path, start, first, "class", node.name, _slice(lines, start, first)))
                for m in methods:
                    ms, me = _node_start(m), m.end_lineno or m.lineno
                    chunks.append(Chunk(path, ms, me, "method", f"{node.name}.{m.name}", _slice(lines, ms, me)))
            else:
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                chunks.append(Chunk(path, start, end, kind, node.name, _slice(lines, start, end)))
        else:
            header.append(node)
    flush_header()
    return chunks


_PY_TOP = re.compile(r"^(?:@|def |async def |class )")


def python_fallback_chunks(path: str, text: str) -> list[Chunk]:
    """For files that do not parse (the syntax bug!): split at top-level def/class/decorators."""
    lines = text.splitlines()
    starts = [1]
    for i, line in enumerate(lines, 1):
        if _PY_TOP.match(line) and i > 1 and not _PY_TOP.match(lines[i - 2]):
            starts.append(i)
    chunks = []
    for a, b in zip(starts, starts[1:] + [len(lines) + 1]):
        body = _slice(lines, a, b - 1)
        if not body.strip():
            continue
        m = re.search(r"^(?:async def|def|class) (\w+)", body, re.M)
        if m is None:
            kind, name = "module", "module header"
        else:
            kind, name = ("class" if m.group(0).startswith("class") else "function"), m.group(1)
        chunks.append(Chunk(path, a, b - 1, kind, name, body.rstrip()))
    return chunks


def _split_at(path: str, text: str, is_start, kind: str, name_of) -> list[Chunk]:
    lines = text.splitlines()
    starts = [1] + [i for i, ln in enumerate(lines, 1) if i > 1 and is_start(ln)]
    out = []
    for a, b in zip(starts, starts[1:] + [len(lines) + 1]):
        body = _slice(lines, a, b - 1)
        if body.strip():
            out.append(Chunk(path, a, b - 1, kind, name_of(lines[a - 1]) or "preamble", body.rstrip()))
    return out


def yaml_chunks(path: str, text: str) -> list[Chunk]:
    key = re.compile(r"^([A-Za-z_\"'][^:#]*):")
    return _split_at(path, text, key.match, "section",
                     lambda ln: (key.match(ln).group(1).strip("\"'") if key.match(ln) else ""))


def toml_chunks(path: str, text: str) -> list[Chunk]:
    sec = re.compile(r"^\[+([^\]]+)\]+")
    return _split_at(path, text, sec.match, "section",
                     lambda ln: (sec.match(ln).group(1) if sec.match(ln) else ""))


def markdown_chunks(path: str, text: str) -> list[Chunk]:
    return _split_at(path, text, lambda ln: ln.startswith("#"), "section",
                     lambda ln: ln.lstrip("#").strip() if ln.startswith("#") else "")


def block_chunks(path: str, text: str, kind: str = "block") -> list[Chunk]:
    """Blank-line separated blocks (Dockerfile instruction groups, plain text)."""
    lines = text.splitlines()
    out, start = [], None
    for i, line in enumerate(lines + [""], 1):
        if line.strip() and start is None:
            start = i
        elif not line.strip() and start is not None:
            first = lines[start - 1].split()[0] if lines[start - 1].split() else ""
            out.append(Chunk(path, start, i - 1, kind, first, _slice(lines, start, i - 1)))
            start = None
    return out


def chunk_file(path: str, text: str) -> list[Chunk]:
    if not text.strip():
        return []
    name = path.rsplit("/", 1)[-1]
    if name.endswith(".py"):
        try:
            chunks = python_chunks(path, text)
        except SyntaxError:
            chunks = python_fallback_chunks(path, text)
    elif name.endswith((".yml", ".yaml")):
        chunks = yaml_chunks(path, text)
    elif name.endswith(".toml"):
        chunks = toml_chunks(path, text)
    elif name.endswith(".md"):
        chunks = markdown_chunks(path, text)
    elif name == "Dockerfile" or name.startswith("Dockerfile."):
        chunks = block_chunks(path, text, "instructions")
    else:
        chunks = block_chunks(path, text)
    return chunks or [Chunk(path, 1, len(text.splitlines()), "block", name, text.rstrip())]
