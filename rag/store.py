"""
store.py — tiny local vector store backed by SQLite.

Zero external infra and zero third-party deps: a single .db file on disk
holds every chunk and its embedding, and cosine search runs in-process with
pure-Python math. Fine for the size of a single Confluence space (thousands
of chunks); swap for a real vector DB if you ever outgrow it.

The DB file lives under rag/.data/ by default (gitignored) — it may hold
client-confidential content, so it must never be committed or exposed.
"""

import math
import os
import sqlite3
import struct
import threading
from dataclasses import dataclass

DATA_DIR = os.environ.get(
    "RAG_DATA_DIR",
    os.path.join(os.path.dirname(__file__), ".data"),
)
DB_PATH = os.path.join(DATA_DIR, "rag.db")


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom else 0.0


@dataclass
class Hit:
    source: str
    title: str
    url: str
    content: str
    score: float


class VectorStore:
    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        # Shared across request threads (ThreadingHTTPServer); serialize with
        # a lock since a single sqlite connection is not thread-safe.
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                source    TEXT NOT NULL,
                title     TEXT,
                url       TEXT,
                chunk_idx INTEGER,
                content   TEXT NOT NULL,
                embedding BLOB NOT NULL
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)"
        )
        self.conn.commit()

    def delete_source(self, source: str) -> int:
        """Remove all chunks for a source (used to re-index a page cleanly)."""
        with self._lock:
            cur = self.conn.execute("DELETE FROM chunks WHERE source = ?", (source,))
            self.conn.commit()
            return cur.rowcount

    def add_chunks(self, source: str, title: str, url: str,
                   chunks: list[str], embeddings: list[list[float]]) -> int:
        rows = [
            (source, title, url, i, chunk, _pack(emb))
            for i, (chunk, emb) in enumerate(zip(chunks, embeddings))
        ]
        with self._lock:
            self.conn.executemany(
                "INSERT INTO chunks (source, title, url, chunk_idx, content, embedding)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()
        return len(rows)

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def sources(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(DISTINCT source) FROM chunks"
            ).fetchone()[0]

    def search(self, query_vec: list[float], top_k: int = 5,
               min_score: float = 0.0) -> list[Hit]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT source, title, url, content, embedding FROM chunks"
            ).fetchall()
        if not rows:
            return []

        scored: list[Hit] = []
        for source, title, url, content, blob in rows:
            score = _cosine(query_vec, _unpack(blob))
            if score >= min_score:
                scored.append(Hit(source, title or "", url or "", content, score))

        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:top_k]
