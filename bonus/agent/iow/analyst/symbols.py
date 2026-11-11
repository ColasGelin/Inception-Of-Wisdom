"""Per-file symbol table: what a file defines, imports, and which env vars it reads.

This is how the Analyst answers "who defines greet?", "who imports helpers?",
"who reads APP_MAX_NAME_LENGTH?" without asking the model anything.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field

_ENV = re.compile(r"""(?:environ\s*\[\s*|environ\.get\(\s*|getenv\(\s*)["']([A-Za-z_][A-Za-z0-9_]*)["']""")
_DEF = re.compile(r"^\s*(?:async\s+def|def|class)\s+(\w+)", re.M)
_IMPORT = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import\s+([\w, ()*]+)|import\s+([\w., ]+))", re.M)
_ASSIGN = re.compile(r"^([A-Z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=", re.M)


@dataclass
class Symbols:
    module: str = ""
    defines: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)   # modules and imported names
    env: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def module_name(path: str) -> str:
    if not path.endswith(".py"):
        return ""
    mod = path[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


def _uniq(items: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in items if i))


def extract(path: str, text: str) -> Symbols:
    s = Symbols(module=module_name(path), env=_uniq(_ENV.findall(text)))
    if not path.endswith(".py"):
        return s
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Broken file: regexes still find the names, which is what lookups need.
        s.defines = _uniq(_DEF.findall(text) + _ASSIGN.findall(text))
        for frm, names, plain in _IMPORT.findall(text):
            if frm:
                s.imports += [frm] + [n.strip(" ()").split(" as ")[0] for n in names.split(",")]
            else:
                s.imports += [n.strip().split(" as ")[0] for n in plain.split(",")]
        s.imports = _uniq(s.imports)
        return s
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            s.defines.append(node.name)
        elif isinstance(node, ast.Import):
            s.imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            s.imports += [node.module or ""] + [a.name for a in node.names]
    for node in tree.body:  # module-level names (constants, settings)
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        s.defines += [t.id for t in targets if isinstance(t, ast.Name)]
    s.defines, s.imports = _uniq(s.defines), _uniq(s.imports)
    return s


class SymbolTable:
    def __init__(self, table: dict[str, Symbols] | None = None):
        self.table = table or {}

    def definers(self, name: str) -> list[str]:
        short = name.rsplit(".", 1)[-1]
        return [p for p, s in self.table.items()
                if short in s.defines or s.module == name or s.module.endswith("." + name)]

    def importers(self, name: str) -> list[str]:
        return [p for p, s in self.table.items()
                if name in s.imports or any(i.endswith("." + name) for i in s.imports)]

    def env_readers(self, var: str) -> list[str]:
        return [p for p, s in self.table.items() if var in s.env]
