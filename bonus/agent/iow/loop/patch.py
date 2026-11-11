"""Patch format, generation, sanity checks and atomic apply.

Format (same as Inception-of-Context):
    {"summary": str, "files": [{"path": str, "op": "create|modify|delete", "content": str}]}
`content` is the complete post-change file. Diffs are not accepted: a small
model mangles hunk headers, and a full file can be checked (syntax, size,
placeholders) before anything touches the disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from iow.analyst.diagnose import CHUNK_CLOSE, Llm

MAX_FILES = 3
MAX_SHRINK = 0.60          # a file may lose at most 60 % of its bytes
TRIVIAL_BYTES = 20         # below this, a replacement for a non-empty file is a placeholder
PROTECTED = {"iow.config.yml"}

# Our own prompt delimiters. If any of these reach a file, the model copied
# the prompt scaffolding instead of writing code.
_MARKERS = re.compile(
    r"<<<\s*(?:END\s+)?(?:CHUNK|FILE)\b|" + re.escape(CHUNK_CLOSE) +
    r"|^(?:CRASH LOG|CANDIDATE FILES|RELEVANT CODE)\b"
    r"|^(?:FACTS FROM THE CODE INDEX|PREVIOUS ATTEMPT|DIAGNOSIS|SUMMARY|ERROR|FAILING LINE):"
    r"|^CURRENT \S+ \(\d+ lines\):", re.M)
_GUTTER = re.compile(r"^\s*\d+\| ", re.M)  # line-number gutter from the diagnosis prompt
_PLACEHOLDER_LINE = re.compile(
    r"^\s*(?:#|//)\s*(?:\.\.\.|…|\(?(?:the )?rest of|remaining (?:code|file)|unchanged|"
    r"existing code|same as (?:before|above)|keep (?:the )?(?:rest|existing)|todo\b|"
    r"your code here|implementation (?:here|omitted)|code omitted)", re.I | re.M)
_PLACEHOLDER_WHOLE = {"pass", "...", "…", "# todo", "todo", "# ...", "none", "null", ""}


class FileChange(BaseModel):
    path: str
    op: Literal["create", "modify", "delete"]
    content: str = ""


class Patch(BaseModel):
    summary: str
    files: list[FileChange]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


def strip_fences(content: str) -> str:
    """Remove an accidental Markdown code fence around a whole file."""
    lines = content.strip("\n").splitlines()
    if len(lines) >= 2 and lines[0].lstrip().startswith("```") and lines[-1].strip() == "```":
        lines = lines[1:-1]
        return "\n".join(lines) + "\n"
    return content


def normalise(patch: Patch) -> Patch:
    files = []
    for f in patch.files:
        content = strip_fences(f.content)
        if f.op != "delete" and content and not content.endswith("\n"):
            content += "\n"
        path = f.path.strip()
        files.append(FileChange(path=path[2:] if path.startswith("./") else path, op=f.op,
                                content=content))
    return Patch(summary=patch.summary.strip(), files=files)


def safe_path(root: Path, rel: str) -> str | None:
    """None when `rel` is a safe relative path inside the target tree, else why not."""
    if not rel or rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        return "absolute path"
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if ".." in parts:
        return "path escapes the target tree (..)"
    if ".git" in parts:
        return "touches .git/"
    if parts and parts[-1] in PROTECTED:
        return "iow.config.yml belongs to the operator, never to patches"
    real_root = root.resolve()
    target = (root / rel).resolve()
    if real_root != target and real_root not in target.parents:
        return "resolves outside the target tree (symlink?)"
    return None


def is_placeholder(content: str) -> str | None:
    stripped = content.strip()
    if stripped.lower() in _PLACEHOLDER_WHOLE:
        return f"content is just {stripped!r}"
    if len(stripped.encode()) < TRIVIAL_BYTES:
        return f"content is only {len(stripped.encode())} bytes"
    m = _PLACEHOLDER_LINE.search(content)
    if m:
        return f"placeholder line {m.group(0).strip()!r}"
    if re.search(r"^\s*\.\.\.\s*$", content, re.M):
        return "a bare '...' line stands in for elided code"
    return None


def sanity_checks(patch: Patch, root: Path) -> list[Check]:
    """Every check, passed or not, so the dashboard and exports can show them all."""
    checks = [Check("at most 3 files", 1 <= len(patch.files) <= MAX_FILES,
                    f"{len(patch.files)} file(s)"),
              Check("summary present", bool(patch.summary.strip()), "")]
    seen: set[str] = set()
    for f in patch.files:
        where = f.path
        reason = safe_path(root, f.path)
        checks.append(Check(f"safe path: {where}", reason is None, reason or "inside the target tree"))
        if reason is not None:
            continue
        if f.path in seen:
            checks.append(Check(f"unique path: {where}", False, "file listed twice"))
            continue
        seen.add(f.path)
        full = root / f.path
        exists = full.is_file()
        if f.op == "create" and exists:
            checks.append(Check(f"op: {where}", False, "create on an existing file"))
            continue
        if f.op in ("modify", "delete") and not exists:
            checks.append(Check(f"op: {where}", False, f"{f.op} on a missing file"))
            continue
        checks.append(Check(f"op: {where}", True, f.op))
        if f.op == "delete":
            continue
        old = full.read_bytes() if exists else b""
        new = f.content.encode()
        if exists:
            checks.append(Check(f"changes something: {where}", new != old,
                                "identical to the current file" if new == old else "content differs"))
        placeholder = is_placeholder(f.content) if old.strip() else None
        checks.append(Check(f"no placeholder: {where}", placeholder is None, placeholder or "real content"))
        if old:
            ratio = len(new) / len(old)
            checks.append(Check(f"shrink <= 60%: {where}", ratio >= 1 - MAX_SHRINK,
                                f"{len(old)} -> {len(new)} bytes ({(1 - ratio) * 100:.0f}% smaller)"
                                if ratio < 1 else f"{len(old)} -> {len(new)} bytes"))
        leak = bool(_MARKERS.search(f.content)) or len(_GUTTER.findall(f.content)) >= 2
        checks.append(Check(f"no retrieval markers: {where}", not leak,
                            "prompt delimiters or line-number gutter in content" if leak else "clean"))
        if f.path.endswith(".py"):
            try:
                compile(f.content, f.path, "exec")
                checks.append(Check(f"python parses: {where}", True, ""))
            except SyntaxError as exc:
                checks.append(Check(f"python parses: {where}", False, f"line {exc.lineno}: {exc.msg}"))
    return checks


def failed(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.passed]


def apply_patch(patch: Patch, root: Path) -> list[str]:
    """Write every file to a temp sibling first, then os.replace them in one go.

    os.replace is atomic per file, so no file is ever half-written. Across
    files the commit (or a git reset) is the unit of atomicity.
    """
    staged: list[tuple[Path, Path | None]] = []
    try:
        for f in patch.files:
            full = root / f.path
            if f.op == "delete":
                staged.append((full, None))
                continue
            full.parent.mkdir(parents=True, exist_ok=True)
            tmp = full.with_name(f".{full.name}.iow-tmp")
            tmp.write_text(f.content, encoding="utf-8")
            if full.exists():
                os.chmod(tmp, full.stat().st_mode & 0o7777)
            staged.append((full, tmp))
    except Exception:
        for _, tmp in staged:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        raise
    for full, tmp in staged:
        if tmp is None:
            full.unlink()
        else:
            os.replace(tmp, full)
    return [f.path for f in patch.files]


# ---- generation ----------------------------------------------------------
# The model writes each file as plain text in a code block; the agent builds
# the patch JSON around it. Asking a 1.5B model to put code inside a JSON
# string fails on the first unescaped quote (observed: `return {"` closed the
# string and truncated the file), so code never travels inside JSON.
SYSTEM = (
    "You fix crashes in a small Python web service. Reply with exactly two parts: first one line "
    "'SUMMARY: <what you changed, one sentence>', then the complete corrected file inside one "
    "``` code block: every line of the file, not only the changed part, no placeholders, no '...'. "
    "Change as little as possible and keep everything else exactly as it is. You can only edit "
    "source files: you cannot install packages or change the environment. If this file does not "
    "need to change, return it unchanged.")

_BLOCK = re.compile(r"```[^\n]*\n(.*?)^\s*```", re.S | re.M)  # the fence line may carry a comment
_SUMMARY = re.compile(r"^\s*SUMMARY:\s*(.+)$", re.M | re.I)


class TextLlm(Llm, Protocol):  # the patch step needs plain text as well as structured JSON
    def chat_text(self, system: str, user: str) -> tuple[str, dict]: ...


def index_facts(diagnosis: dict[str, Any]) -> list[str]:
    facts = []
    for c in diagnosis.get("candidates", []):
        hard = [r for r in c["reasons"] if not r.startswith("retrieval")]
        if hard:
            facts.append(f"{c['path']}: {'; '.join(hard)}")
    return facts


def build_file_prompt(diagnosis: dict[str, Any], path: str, content: str, feedback: str = "") -> str:
    # Order matters for a 1.5B model: the file first, then the precise facts right before the
    # instruction, where they weigh most (observed: facts placed before the file were ignored).
    clues = diagnosis.get("clues", {})
    lang = "python" if path.endswith(".py") else ""
    parts = [f"ERROR:\n{clues.get('condensed') or ''}", f"DIAGNOSIS: {diagnosis['summary']}",
             f"CURRENT {path} ({len(content.splitlines())} lines):\n```{lang}\n{content.rstrip()}\n```"]
    frames = clues.get("target_frames") or []
    if frames and frames[-1]["path"] == path and frames[-1].get("code"):
        parts.append(f"FAILING LINE: {path} line {frames[-1]['line']}: "
                     f"`{frames[-1]['code'].splitlines()[0]}`")
    facts = diagnosis.get("patch_facts") or diagnosis.get("facts") or index_facts(diagnosis)
    if facts:
        parts.append("FACTS FROM THE CODE INDEX:\n" + "\n".join(f"- {f}" for f in facts))
    if feedback:
        parts.append(f"PREVIOUS ATTEMPT: {feedback}")
    parts.append(f"Using these facts, write the complete fixed {path}.")
    return "\n\n".join(parts)


def parse_file_answer(text: str) -> tuple[str, str]:
    """(summary, file content) from 'SUMMARY: ...' + one code block."""
    m = _BLOCK.search(text)
    if not m:
        raise ValueError("the answer has no code block")
    s = _SUMMARY.search(text[:m.start()]) or _SUMMARY.search(text)
    return (s.group(1).strip() if s else ""), m.group(1)


def propose_patch(diagnosis: dict[str, Any], root: Path, llm: TextLlm, feedback: str = "") -> tuple[Patch, dict, str]:
    """One plain-text call per suspect file (max 3); unchanged files are dropped.

    Raises ValueError when the model's answers do not form a usable patch.
    """
    changes, summaries, prompts = [], [], []
    stats: dict[str, Any] = {"calls": []}
    for path in diagnosis["files"][:MAX_FILES]:
        content = (root / path).read_text(encoding="utf-8")
        prompt = build_file_prompt(diagnosis, path, content, feedback)
        prompts.append(prompt)
        answer, call = llm.chat_text(SYSTEM, prompt)
        stats["calls"].append({k: v for k, v in call.items() if k != "raw"} | {"path": path})
        summary, new = parse_file_answer(answer)
        if new.rstrip("\n") != content.rstrip("\n"):
            changes.append({"path": path, "op": "modify", "content": new})
            summaries.append(summary or f"edited {path}")
    if not changes:
        raise ValueError("the model returned every suspect file unchanged")
    patch = normalise(Patch.model_validate({"summary": " ".join(summaries), "files": changes}))
    stats["model"] = diagnosis.get("llm", {}).get("model")
    return patch, stats, "\n\n=====\n\n".join(prompts)
