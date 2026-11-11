"""Persistent ChromaDB index of the target's source tree.

Two collections in one PersistentClient:
- `code_chunks`: one record per logical chunk, embedded with the local model.
- `code_files`:  one record per file = the manifest (content hash) + its symbol table.

On start the manifest is compared with the disk and only changed files are
re-embedded; nothing is ever wiped on start. The only full re-embed happens
when the embedding model named in the collection metadata changes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

import chromadb
from chromadb.config import Settings

from iow.analyst.chunker import Chunk, chunk_file
from iow.analyst.symbols import SymbolTable, Symbols, extract

CHUNKS, FILES = "code_chunks", "code_files"
MAX_FILE_BYTES = 200_000
EMBED_CHARS = 4000  # what the embedder sees of a chunk; the full text is stored

Embedder = Callable[[list[str]], list[list[float]]]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class CodeIndex:
    def __init__(self, db_path: Path, root: Path, embed: Embedder, embedding_model: str,
                 exclude: list[str], log: Callable[[str], None] = print):
        self.db_path, self.root = db_path, root
        self._embed, self.embedding_model = embed, embedding_model
        self.exclude = exclude
        self._log = log
        self._lock = threading.RLock()
        self.client = chromadb.PersistentClient(path=str(db_path),
                                                settings=Settings(anonymized_telemetry=False))
        self.last_sync: dict | None = None
        self._open()

    # ---- collections -------------------------------------------------
    def _open(self) -> None:
        meta = {"hnsw:space": "cosine", "embedding_model": self.embedding_model}
        self.chunks = self.client.get_or_create_collection(CHUNKS, embedding_function=None, metadata=meta)
        stored = (self.chunks.metadata or {}).get("embedding_model")
        if stored != self.embedding_model:
            # The only case where vectors are thrown away: they are not comparable across models.
            self._log(f"embedding model changed ({stored} -> {self.embedding_model}): re-embedding everything")
            self.client.delete_collection(CHUNKS)
            self.client.delete_collection(FILES)
            self.chunks = self.client.get_or_create_collection(CHUNKS, embedding_function=None,
                                                               metadata=meta)
        self.files = self.client.get_or_create_collection(FILES, embedding_function=None)

    # ---- manifest ----------------------------------------------------
    def manifest(self) -> dict[str, str]:
        with self._lock:
            got = self.files.get(include=["metadatas"])
        return {i: m["hash"] for i, m in zip(got["ids"], got["metadatas"])}

    def symbol_table(self) -> SymbolTable:
        with self._lock:
            got = self.files.get(include=["metadatas"])
        return SymbolTable({i: Symbols(**json.loads(m["symbols"]))
                            for i, m in zip(got["ids"], got["metadatas"])})

    def excluded(self, rel: str) -> bool:
        parts = rel.split("/")
        return any(fnmatch.fnmatch(rel, pat) or pat in parts or fnmatch.fnmatch(parts[-1], pat)
                   for pat in self.exclude)

    def scan(self) -> dict[str, bytes]:
        """Text files of the target tree, by relative path."""
        out: dict[str, bytes] = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            rel_dir = os.path.relpath(dirpath, self.root)
            rel_dir = "" if rel_dir == "." else rel_dir + "/"
            dirnames[:] = [d for d in dirnames if not self.excluded(rel_dir + d)]
            for fn in filenames:
                rel = rel_dir + fn
                if self.excluded(rel):
                    continue
                full = Path(dirpath) / fn
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                    data = full.read_bytes()
                    data.decode("utf-8")
                except (OSError, UnicodeDecodeError):
                    continue  # binary or vanished mid-scan
                out[rel] = data
        return out

    # ---- incremental sync ----------------------------------------------
    def sync(self) -> dict:
        """Bring the index in line with the disk, touching only what differs."""
        t0 = time.time()
        with self._lock:
            disk = self.scan()
            known = self.manifest()
            changed = sorted(p for p, data in disk.items() if known.get(p) != sha256(data))
            removed = sorted(set(known) - set(disk))
            for path in removed:
                self._remove(path)
            n_chunks = 0
            for path in changed:
                n_chunks += self._index_file(path, disk[path])
        stats = {"files": len(disk), "changed": changed, "removed": removed,
                 "chunks_embedded": n_chunks, "elapsed_s": round(time.time() - t0, 2), "at": time.time()}
        self.last_sync = stats
        return stats

    def _remove(self, path: str) -> None:
        self.chunks.delete(where={"path": path})
        self.files.delete(ids=[path])

    def _index_file(self, path: str, data: bytes) -> int:
        text = data.decode("utf-8")
        chunks = chunk_file(path, text)
        self.chunks.delete(where={"path": path})
        if chunks:
            docs = [f"search_document: {c.path} {c.kind} {c.name}\n{c.text[:EMBED_CHARS]}" for c in chunks]
            vectors = self._embed(docs)
            self.chunks.upsert(
                ids=[c.id for c in chunks], embeddings=vectors, documents=[c.text for c in chunks],
                metadatas=[{"path": c.path, "start": c.start, "end": c.end, "kind": c.kind,
                            "name": c.name} for c in chunks])
        # Manifest last: if we die before this line, the file is simply redone next sync.
        self.files.upsert(ids=[path], embeddings=[[0.0]], documents=[path],
                          metadatas=[{"hash": sha256(data), "chunks": len(chunks),
                                      "symbols": json.dumps(extract(path, text).to_dict()),
                                      "indexed_at": time.time()}])
        return len(chunks)

    # ---- queries -----------------------------------------------------
    def search(self, query: str, k: int, path: str | None = None) -> list[dict]:
        with self._lock:
            total = self.chunks.count()
            if total == 0 or not query.strip():
                return []
            [vector] = self._embed([f"search_query: {query}"])
            res = self.chunks.query(query_embeddings=[vector], n_results=min(k, total),
                                    where={"path": path} if path else None,
                                    include=["documents", "metadatas", "distances"])
        return [{**m, "text": d, "score": round(1.0 - dist, 4), "id": f"{m['path']}:{m['start']}-{m['end']}"}
                for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])]

    def chunks_of(self, path: str) -> list[Chunk]:
        with self._lock:
            got = self.chunks.get(where={"path": path}, include=["documents", "metadatas"])
        out = [Chunk(m["path"], m["start"], m["end"], m["kind"], m["name"], d)
               for d, m in zip(got["documents"], got["metadatas"])]
        return sorted(out, key=lambda c: c.start)

    def paths(self) -> list[str]:
        return sorted(self.manifest())

    def stats(self) -> dict:
        with self._lock:
            return {"files": self.files.count(), "chunks": self.chunks.count(),
                    "embedding_model": self.embedding_model, "last_sync": self.last_sync}
