from iow.analyst.symbols import SymbolTable, extract, module_name

CONFIG = 'import os\nAPP = os.environ.get("APP_NAME", "x")\nN = int(os.environ["APP_MAX"])\n'
TEXT = "def greet(name):\n    return name\n"
MAIN = "from helpers import greet\nfrom app.routes import router\n"


def test_module_name():
    assert module_name("app/text.py") == "app.text"
    assert module_name("app/__init__.py") == "app"


def test_extract_defs_imports_env():
    s = extract("app/config.py", CONFIG)
    assert {"APP", "N"} <= set(s.defines) and s.env == ["APP_NAME", "APP_MAX"]
    m = extract("app/main.py", MAIN)
    assert {"helpers", "greet", "app.routes", "router"} <= set(m.imports)


def test_extract_survives_syntax_errors():
    s = extract("app/routes.py", "from app.text import greet\n\ndef health()\n    return 1\n")
    assert "health" in s.defines and "greet" in s.imports


def test_lookups():
    t = SymbolTable({p: extract(p, src) for p, src in
                     [("app/config.py", CONFIG), ("app/text.py", TEXT), ("app/main.py", MAIN)]})
    assert t.definers("greet") == ["app/text.py"]
    assert t.importers("helpers") == ["app/main.py"]
    assert t.env_readers("APP_MAX") == ["app/config.py"]
    assert t.definers("app.text") == ["app/text.py"]
