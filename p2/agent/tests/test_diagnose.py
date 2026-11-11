import pytest
from fakes import FakeEmbedder, FakeLlm

from iow.analyst.diagnose import diagnose, map_path, parse_log, schema_for, validate
from iow.analyst.index import CodeIndex

KNOWN = ["app/main.py", "app/text.py", "app/routes.py", "app/config.py"]

IMPORT_LOG = '''Traceback (most recent call last):
  File "/usr/local/lib/python3.12/site-packages/uvicorn/importer.py", line 19, in import_from_string
    module = importlib.import_module(module_str)
  File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
  File "/app/app/main.py", line 3, in <module>
    from helpers import greet
ModuleNotFoundError: No module named 'helpers\''''

SYNTAX_LOG = '''  File "/app/app/main.py", line 6, in <module>
    from app.routes import router
  File "/app/app/routes.py", line 17
    def health() -> dict[str, str]
                                  ^
SyntaxError: expected ':\''''

ENV_LOG = '''  File "/app/app/config.py", line 7, in <module>
    MAX_NAME_LENGTH = int(os.environ["APP_MAX_NAME_LENGTH"])
                      ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
ValueError: invalid literal for int() with base 10: 'twenty\''''


def test_map_path_strips_container_prefix():
    assert map_path("/app/app/main.py", KNOWN) == "app/main.py"
    assert map_path("/usr/local/lib/python3.12/site-packages/uvicorn/importer.py", KNOWN) is None
    assert map_path("<frozen importlib._bootstrap>", KNOWN) is None


def test_parse_import_error():
    c = parse_log(IMPORT_LOG, KNOWN)
    assert (c.exc_type, c.message) == ("ModuleNotFoundError", "No module named 'helpers'")
    assert [(f["path"], f["line"]) for f in c.target_frames] == [("app/main.py", 3)]
    assert "helpers" in c.identifiers and "greet" in c.identifiers
    assert "/usr/local" not in c.condensed and "app/main.py" in c.condensed


def test_parse_syntax_error_frames_and_identifiers():
    c = parse_log(SYNTAX_LOG, KNOWN)
    assert [f["path"] for f in c.target_frames] == ["app/main.py", "app/routes.py"]
    assert "health" in c.identifiers and "app.routes" in c.identifiers


def test_parse_env_vars():
    c = parse_log(ENV_LOG, KNOWN)
    assert "APP_MAX_NAME_LENGTH" in c.env_vars and "MAX_NAME_LENGTH" in c.identifiers


def test_schema_restricts_files_to_candidates():
    s = schema_for(["a.py", "b.py"])
    assert s["properties"]["files"]["items"]["enum"] == ["a.py", "b.py"]


def test_validate_rejects_unusable_answers():
    allowed, indexed = ["app/main.py"], {"app/main.py"}
    assert isinstance(validate({"summary": "", "files": ["app/main.py"]}, allowed, indexed), str)
    assert isinstance(validate({"summary": "a real explanation", "files": []}, allowed, indexed), str)
    assert "not indexed" in validate({"summary": "a real explanation", "files": ["app/ghost.py"]},
                                     allowed, indexed)
    assert validate({"summary": "a real explanation", "files": ["app/main.py", "app/main.py"]},
                    allowed, indexed) == (["app/main.py"], "a real explanation")


@pytest.fixture
def index(tmp_path, git_repo):
    (git_repo / "app" / "main.py").write_text("from fastapi import FastAPI\n\nfrom helpers import greet\n\napp = FastAPI()\n")
    ix = CodeIndex(tmp_path / "chroma", git_repo, FakeEmbedder(), "fake", [".git"])
    ix.sync()
    return ix


def test_diagnosis_names_the_traceback_file(index):
    llm = FakeLlm({"summary": "main.py imports greet from a module that does not exist.",
                   "files": ["app/main.py"]})
    d = diagnose(IMPORT_LOG, index, llm, top_k=5, max_candidates=4)
    assert d["ok"] and d["files"] == ["app/main.py"]
    cands = [c["path"] for c in d["candidates"]]
    assert cands[0] == "app/main.py" and "app/text.py" in cands  # text.py defines greet
    assert "1. app/main.py" in llm.prompts[0] and "<<<CHUNK app/main.py:" in llm.prompts[0]


@pytest.mark.parametrize("answer, stage", [
    ({"summary": "", "files": []}, "validate"),
    ({"summary": "something is wrong somewhere", "files": []}, "validate"),
    ({"summary": "the bug is in a made-up file", "files": ["app/ghost.py"]}, "validate"),
    ({}, "validate"),
    (RuntimeError("connection refused"), "llm"),
])
def test_unusable_model_answer_is_an_explicit_failure(index, answer, stage):
    d = diagnose(IMPORT_LOG, index, FakeLlm(answer), top_k=5, max_candidates=4)
    assert d["ok"] is False and d["stage"] == stage and d["reason"]
    assert "files" not in d and "summary" not in d  # nothing for Part 3 to act on


def test_no_candidates_means_no_llm_call(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    ix = CodeIndex(tmp_path / "c2", empty, FakeEmbedder(), "fake", [".git"])
    ix.sync()
    llm = FakeLlm({"summary": "x" * 20, "files": ["x"]})
    d = diagnose(IMPORT_LOG, ix, llm, top_k=5, max_candidates=4)
    assert d["ok"] is False and d["stage"] == "candidates" and llm.prompts == []


def test_retrieval_alone_is_not_evidence(index):
    llm = FakeLlm({"summary": "x" * 20, "files": ["app/main.py"]})
    d = diagnose("RuntimeError: worker crashed unexpectedly\n", index, llm, top_k=5, max_candidates=4)
    assert d["ok"] is False and d["stage"] == "evidence" and llm.prompts == []
