"""Crash event -> structured diagnosis {summary, files}.

Small-model friendly: the index does the finding, the model only chooses.
1. parse the log: exception, traceback frames, identifiers, env vars, modules
2. map frames to indexed files, look identifiers up in the symbol table
3. retrieval with a query built from the exception + identifiers
4. rank -> a short numbered candidate list + the relevant chunks
5. the LLM picks from that list: the JSON schema makes `files` an enum of the
   candidates, so it cannot invent a path; we validate again anyway.
"""

from __future__ import annotations

import keyword
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from iow.analyst.chunker import Chunk
from iow.analyst.symbols import SymbolTable
from iow.observer.events import find_exception

_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>[^\s]+))?')
_QUOTED = re.compile(r"""['"]([A-Za-z_][\w.]*)['"]""")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_FROM_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(.+)$")
_STOP = set(keyword.kwlist) | {
    "self", "cls", "None", "True", "False", "str", "int", "dict", "list", "bool", "float", "print",
    "return", "app", "module", "line", "File", "name", "named", "No", "from", "import", "with",
    "base", "invalid", "literal", "for", "the", "expected", "object", "has", "attribute", "not",
    "defined", "cannot", "is", "of", "in", "a", "an", "to", "get", "post", "router", "Traceback",
    "most", "recent", "call", "last", "os", "environ", "fastapi", "FastAPI", "APIRouter"}
CHUNK_OPEN, CHUNK_CLOSE = "<<<CHUNK {id}>>>", "<<<END CHUNK>>>"
SUMMARY_MAX = 600


class Index(Protocol):
    def paths(self) -> list[str]: ...
    def symbol_table(self) -> SymbolTable: ...
    def search(self, query: str, k: int, path: str | None = None) -> list[dict]: ...
    def chunks_of(self, path: str) -> list[Chunk]: ...


class Llm(Protocol):
    def chat_json(self, system: str, user: str, schema: dict) -> tuple[dict, dict]: ...


@dataclass
class Clues:
    exc_type: str = ""
    message: str = ""
    frames: list[dict] = field(default_factory=list)       # every frame, raw
    target_frames: list[dict] = field(default_factory=list)  # frames inside the target tree
    identifiers: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    condensed: str = ""


def map_path(raw: str, known: list[str]) -> str | None:
    """Container path -> indexed relative path (`/app/app/main.py` -> `app/main.py`)."""
    raw = raw.replace("\\", "/")
    best = None
    for rel in known:
        if raw == rel or raw.endswith("/" + rel):
            if best is None or len(rel) > len(best):
                best = rel
    return best


def parse_log(excerpt: str, known: list[str]) -> Clues:
    lines = excerpt.splitlines()
    clues = Clues()
    exc = find_exception(excerpt)
    if exc:
        clues.exc_type, clues.message = exc
    code_lines: list[str] = []
    keep: list[str] = []
    for i, line in enumerate(lines):
        m = _FRAME.search(line)
        if not m:
            continue
        frame = {"raw_path": m.group("path"), "line": int(m.group("line")), "func": m.group("func"),
                 "path": map_path(m.group("path"), known), "code": ""}
        # The source line printed under a frame (and SyntaxError's caret block).
        follow = []
        for nxt in lines[i + 1:i + 4]:
            if _FRAME.search(nxt) or not nxt.startswith("    "):
                break
            follow.append(nxt)
        frame["code"] = "\n".join(f.strip() for f in follow if f.strip(" ^~"))
        clues.frames.append(frame)
        if frame["path"] and not any(f["path"] == frame["path"] and f["line"] == frame["line"]
                                     for f in clues.target_frames):
            clues.target_frames.append(frame)
            code_lines.append(frame["code"])
            keep += [line.replace(frame["raw_path"], frame["path"])] + follow
    tail = [ln for ln in lines[-4:] if ln.strip() and not _FRAME.search(ln) and ln not in keep]
    clues.condensed = "\n".join(keep + tail) or "\n".join(lines[-15:])

    text = " ".join([clues.message] + code_lines)
    quoted = _QUOTED.findall(clues.message)
    idents = quoted + [t for t in _IDENT.findall(text) if len(t) > 2]
    clues.identifiers = list(dict.fromkeys(i for i in idents if i not in _STOP and not i.isdigit()))[:15]
    clues.env_vars = [i for i in clues.identifiers if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", i)]
    return clues


@dataclass
class Candidate:
    path: str
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def add(self, points: float, reason: str) -> None:
        self.score += points
        if reason not in self.reasons:
            self.reasons.append(reason)


def rank_candidates(clues: Clues, index: Index, top_k: int, limit: int) -> tuple[list[Candidate], list[dict]]:
    cands: dict[str, Candidate] = {}

    def cand(p: str) -> Candidate:
        return cands.setdefault(p, Candidate(p))

    for n, f in enumerate(reversed(clues.target_frames)):
        cand(f["path"]).add(5.0 if n == 0 else 3.0,
                            f"traceback: line {f['line']}" + (" (last frame)" if n == 0 else ""))
    table = index.symbol_table()
    for ident in clues.identifiers:
        for p in table.definers(ident):
            cand(p).add(2.0, f"defines {ident}")
        for p in table.importers(ident):
            cand(p).add(1.5, f"imports {ident}")
    for var in clues.env_vars:
        for p in table.env_readers(var):
            cand(p).add(3.0, f"reads env {var}")
    query = " ".join(filter(None, [clues.exc_type, clues.message] + clues.identifiers))
    try:
        hits = index.search(query, top_k) if query.strip() else []
    except Exception:  # embeddings down: traceback + symbol table still give candidates
        hits = []
    for h in hits:
        cand(h["path"]).add(2.0 * max(h["score"], 0.0), f"retrieval {h['score']:.2f} ({h['name']})")
    ranked = sorted(cands.values(), key=lambda c: c.score, reverse=True)[:limit]
    return ranked, hits


def context_chunks(clues: Clues, cands: list[Candidate], hits: list[dict], index: Index,
                   max_chunks: int = 8) -> list[dict]:
    """Chunks shown to the model: the ones the traceback points into, then retrieval hits."""
    wanted = {c.path for c in cands}
    out: dict[str, dict] = {}
    for f in reversed(clues.target_frames):
        chs = index.chunks_of(f["path"])
        ch = next((c for c in chs if c.start <= f["line"] <= c.end), None) or next(
            (c for c in reversed(chs) if c.start <= f["line"]), None)
        if ch is not None:
            out.setdefault(ch.id, {**ch.to_dict(), "why": f"traceback line {f['line']}", "score": None})
    for h in hits:
        if h["path"] in wanted:
            out.setdefault(h["id"], {**h, "why": "retrieval"})
    return list(out.values())[:max_chunks]


def _numbered(chunk: dict) -> str:
    return "\n".join(f"{chunk['start'] + i:4d}| {ln}" for i, ln in enumerate(chunk["text"].splitlines()))


SYSTEM = (
    "You are a debugging assistant for a small Python web service that crashed on start. "
    "From the numbered candidate files, choose the file or files that must be edited to fix the "
    "crash, and explain in one short paragraph what is wrong and why. Prefer fixing the failing "
    "line itself (the last traceback frame inside the project) over changing or creating other "
    "modules; choose another file only if the fix clearly belongs there. The fix must be a change "
    "to the project's source files: installing packages or changing the environment is not "
    "possible. Only use paths from the candidate list. Reply with JSON only.")


def index_facts(clues: Clues, table: SymbolTable, for_patch: bool = False) -> list[str]:
    """Plain statements the index can prove, so the model does not have to infer them.

    `for_patch` adds where each name of a broken import really lives: what the patch step
    needs, but it pulled the 1.5B model's *choice of file* towards the definer (observed).
    """
    facts = []
    import_error = clues.exc_type in ("ModuleNotFoundError", "ImportError")
    for ident in clues.identifiers[:8]:
        defs = table.definers(ident)
        if defs:
            facts.append(f"`{ident}` is defined in {', '.join(defs)}")
        elif import_error and table.importers(ident):
            facts.append(f"no module or symbol named `{ident}` exists in the project "
                         f"(imported by {', '.join(table.importers(ident))})")
    raised = clues.target_frames[-1] if clues.target_frames else None
    m = _FROM_IMPORT.match(raised["code"].splitlines()[0]) if raised and raised["code"] else None
    if for_patch and m and not any(s.module == m.group(1) for s in table.table.values()):
        # The failing line imports from a module the project does not have: say where each
        # imported name really lives (a symbol-table fact, not a guess).
        for name in (n.strip(" ()").split(" as ")[0] for n in m.group(2).split(",")):
            for d in table.definers(name):
                facts.append(f"the failing line imports `{name}` from `{m.group(1)}`, which does not "
                             f"exist; `{name}` is defined in {d}, importable as "
                             f"`from {table.table[d].module} import {name}`")
    for var in clues.env_vars:
        readers = table.env_readers(var)
        if readers:
            facts.append(f"environment variable {var} is read in {', '.join(readers)}")
    return list(dict.fromkeys(facts))


def build_prompt(clues: Clues, cands: list[Candidate], chunks: list[dict],
                 facts: list[str] | None = None) -> str:
    raised = clues.target_frames[-1] if clues.target_frames else None

    def label(c: Candidate) -> str:
        if raised and c.path == raised["path"]:
            code = raised["code"].splitlines()[0] if raised["code"] else ""
            return f"contains the failing line {raised['line']}" + (f": `{code}`" if code else "")
        return "; ".join(r for r in c.reasons[:3] if not r.startswith("traceback"))

    listing = "\n".join(f"{i}. {c.path}  ({label(c)})" for i, c in enumerate(cands, 1))
    code = "\n\n".join(f"{CHUNK_OPEN.format(id=c['id'])}\n{_numbered(c)}\n{CHUNK_CLOSE}" for c in chunks)
    fact_block = ("FACTS FROM THE CODE INDEX:\n" + "\n".join(f"- {f}" for f in facts) + "\n\n") if facts else ""
    return (f"CRASH LOG (traceback, target files only):\n{clues.condensed}\n\n"
            f"{fact_block}"
            f"CANDIDATE FILES (choose only from these):\n{listing}\n\n"
            f"RELEVANT CODE (line numbers on the left):\n{code}\n\n"
            'Answer as {"summary": "<one short paragraph>", "files": ["<the file(s) to edit>"]}.')


def schema_for(paths: list[str]) -> dict[str, Any]:
    # Summary first: the model reasons, then names the files its own reasoning points to.
    # maxLength: an unbounded string lets a small model ramble until Ollama gives up
    # (observed: done=false after ~300 tokens, i.e. truncated, invalid JSON).
    return {"type": "object", "required": ["summary", "files"],
            "properties": {"summary": {"type": "string", "minLength": 20, "maxLength": SUMMARY_MAX},
                           "files": {"type": "array", "minItems": 1, "maxItems": 3,
                                     "items": {"type": "string", "enum": paths}}}}


def validate(answer: dict, allowed: list[str], indexed: set[str]) -> tuple[list[str], str] | str:
    """(files, summary) when usable, else the reason it is not."""
    summary = answer.get("summary")
    files = answer.get("files")
    if not isinstance(summary, str) or len(summary.strip()) < 10:
        return "the model returned an empty or trivial summary"
    if not isinstance(files, list) or not files:
        return "the model named no suspect file"
    bad = [f for f in files if not isinstance(f, str) or f not in indexed or f not in allowed]
    if bad:
        return f"the model named files that are not indexed candidates: {bad}"
    return list(dict.fromkeys(files)), summary.strip()


def diagnose(excerpt: str, index: Index, llm: Llm, top_k: int, max_candidates: int,
             feedback: str = "") -> dict[str, Any]:
    """Never guesses: any unusable outcome is {"ok": False, "reason": ...}."""
    known = index.paths()
    clues = parse_log(excerpt, known)
    base = {"clues": asdict(clues)}
    if not excerpt.strip():
        return {**base, "ok": False, "stage": "parse", "reason": "the crash event has no log excerpt"}
    cands, hits = rank_candidates(clues, index, top_k, max_candidates)
    base.update(candidates=[asdict(c) for c in cands], retrieval=hits)
    if not cands:
        return {**base, "ok": False, "stage": "candidates",
                "reason": "no indexed file matches anything in the error log"}
    if not any(not r.startswith("retrieval") for c in cands for r in c.reasons):
        # Similar-looking code is not evidence: without a traceback frame or a known
        # identifier pointing into the tree, any pick by the model would be a guess.
        return {**base, "ok": False, "stage": "evidence",
                "reason": "no traceback frame or known identifier in the log points into the "
                          "indexed code; retrieval similarity alone is not evidence"}
    chunks = context_chunks(clues, cands, hits, index)
    base["chunks"] = chunks
    paths = [c.path for c in cands]
    table = index.symbol_table()
    facts = index_facts(clues, table)
    base["facts"], base["patch_facts"] = facts, index_facts(clues, table, for_patch=True)
    prompt = build_prompt(clues, cands, chunks, facts) + (f"\n\nNOTE: {feedback}" if feedback else "")
    try:
        answer, stats = llm.chat_json(SYSTEM, prompt, schema_for(paths))
    except Exception as exc:
        return {**base, "ok": False, "stage": "llm", "reason": f"LLM call failed: {exc}"}
    base.update(llm=stats, answer=answer, prompt=prompt)
    checked = validate(answer, paths, set(known))
    if isinstance(checked, str):
        return {**base, "ok": False, "stage": "validate", "reason": checked}
    files, summary = checked
    raised = clues.target_frames[-1] if clues.target_frames else None
    if raised and raised["path"] in paths and raised["path"] not in files:
        # Consistency check by the index: the model skipped the file holding the failing line.
        # Ask once more with that observation; the model still makes the final choice.
        note = (f"Your previous answer chose {', '.join(files)}, but the failing line is in "
                f"{raised['path']} line {raised['line']}. Check whether that line itself is what "
                "must change.")
        try:
            second, stats2 = llm.chat_json(SYSTEM, f"{prompt}\n\nNOTE: {note}", schema_for(paths))
        except Exception as exc:
            second, stats2 = {}, {"error": str(exc)}
        rechecked = validate(second, paths, set(known))
        base["first_answer"], base["reasked"] = answer, note
        if not isinstance(rechecked, str):
            files, summary = rechecked
            base.update(answer=second, llm=stats2)
    return {**base, "ok": True, "summary": summary, "files": files}
