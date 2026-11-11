import pytest
from fakes import FakeEmbedder

from iow.analyst.index import CodeIndex


@pytest.fixture
def make_index(tmp_path, git_repo):
    def make(embedder=None, model="fake-embed"):
        emb = embedder or FakeEmbedder()
        return CodeIndex(tmp_path / "chroma", git_repo, emb, model,
                         [".git", "__pycache__", "iow.config.yml"]), emb
    return make


def test_first_sync_indexes_everything(make_index):
    index, emb = make_index()
    stats = index.sync()
    assert stats["changed"] == ["app/__init__.py", "app/main.py", "app/text.py"]
    assert index.stats()["files"] == 3 and index.stats()["chunks"] > 0


def test_reopen_reuses_the_index_without_embedding(make_index):
    index, _ = make_index()
    index.sync()
    chunks = index.stats()["chunks"]
    reopened, emb = make_index()  # same directory: a new process after `make down && make up`
    stats = reopened.sync()
    assert stats["changed"] == [] and stats["removed"] == [] and emb.embedded == 0
    assert reopened.stats()["chunks"] == chunks


def test_editing_one_file_reembeds_only_that_file(make_index, git_repo):
    index, emb = make_index()
    index.sync()
    before = emb.embedded
    (git_repo / "app" / "text.py").write_text("def greet(name):\n    return name\n\n\ndef bye():\n    return 1\n")
    stats = index.sync()
    assert stats["changed"] == ["app/text.py"]
    assert emb.embedded - before == 2  # two chunks, both from text.py
    assert all("text.py" in t for t in emb.calls[-1])


def test_deleting_a_file_removes_its_chunks(make_index, git_repo):
    index, _ = make_index()
    index.sync()
    (git_repo / "app" / "text.py").unlink()
    stats = index.sync()
    assert stats["removed"] == ["app/text.py"]
    assert index.chunks_of("app/text.py") == [] and "app/text.py" not in index.paths()


def test_excluded_and_git_files_are_not_indexed(make_index, git_repo):
    (git_repo / "iow.config.yml").write_text("loop: {}\n")
    index, _ = make_index()
    index.sync()
    assert all(not p.startswith(".git") and p != "iow.config.yml" for p in index.paths())


def test_search_returns_scored_chunks(make_index):
    index, _ = make_index()
    index.sync()
    hits = index.search("greet name hello", k=2)
    assert len(hits) == 2 and hits[0]["score"] >= hits[1]["score"]
    assert {"path", "start", "end", "text", "score", "name"} <= set(hits[0])


def test_changing_the_embedding_model_reembeds(make_index):
    index, _ = make_index(model="a")
    index.sync()
    other, emb = make_index(model="b")
    assert other.sync()["changed"] and emb.embedded > 0


def test_symbols_are_stored_with_the_index(make_index):
    index, _ = make_index()
    index.sync()
    table = index.symbol_table()
    assert table.definers("greet") == ["app/text.py"]
