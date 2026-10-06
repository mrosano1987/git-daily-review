"""
service.py — local RAG HTTP service for Git Daily Review.

Implements the contract expected by scripts/rag_client.py plus an /index
endpoint for ingestion. Stdlib only (http.server) — no web framework.

Endpoints:
  GET  /health           → 200 {"status":"ok","chunks":N,"sources":M}
  POST /index            → ingest documents (embed + store)
                           body: {"documents":[{source,title,url,content}]}
  POST /retrieve-context → retrieval for a set of code changes
                           body: {repo,branch,changed_files,diff,query}

Run:
  python3 rag/service.py            # listens on 0.0.0.0:8000
  RAG_PORT=8001 python3 rag/service.py

Everything stays local. The store may hold client-confidential docs — do
not expose this port outside localhost.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from chunking import chunk_text
from embeddings import DEFAULT_MODEL, embed
from store import VectorStore

HOST = os.environ.get("RAG_HOST", "127.0.0.1")
PORT = int(os.environ.get("RAG_PORT", "8000"))
TOP_K = int(os.environ.get("RAG_TOP_K", "5"))
MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.35"))

_store = VectorStore()


def _risk_from_score(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.55:
        return "medium"
    return "low"


def _index(documents: list[dict]) -> dict:
    indexed, total_chunks = 0, 0
    for doc in documents:
        source = doc.get("source") or doc.get("url") or doc.get("title")
        if not source:
            continue
        content = doc.get("content", "")
        chunks = chunk_text(content)
        if not chunks:
            continue
        embeddings = [embed(c) for c in chunks]
        _store.delete_source(source)  # idempotent re-index
        _store.add_chunks(
            source=source,
            title=doc.get("title", ""),
            url=doc.get("url", ""),
            chunks=chunks,
            embeddings=embeddings,
        )
        indexed += 1
        total_chunks += len(chunks)
    return {"indexed": indexed, "chunks": total_chunks,
            "total_chunks": _store.count(), "total_sources": _store.sources()}


def _retrieve(body: dict) -> dict:
    changed_files = body.get("changed_files", []) or []
    query = body.get("query", "") or ""
    diff = body.get("diff", "") or ""

    # Build a retrieval query from the signal we have about the change.
    parts = [query] if query else []
    if changed_files:
        parts.append("Files: " + ", ".join(changed_files[:20]))
    if diff:
        parts.append(diff[:2000])
    query_text = "\n".join(parts) or (body.get("repo", "") + " changes")

    query_vec = embed(query_text)
    hits = _store.search(query_vec, top_k=TOP_K, min_score=MIN_SCORE)

    contexts = [
        {"source": h.title or h.source, "score": round(h.score, 4),
         "source_id": h.source, "url": h.url, "content": h.content}
        for h in hits
    ]
    dependencies = list(dict.fromkeys(
        [h.title or h.source for h in hits]
    ))
    top = hits[0].score if hits else 0.0
    return {
        "contexts": contexts,
        "dependencies": dependencies,
        "risk_score": _risk_from_score(top) if hits else "unknown",
        "summary": (f"{len(contexts)} chunk pertinenti da Confluence"
                    if contexts else ""),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"status": "ok", "model": DEFAULT_MODEL,
                             "chunks": _store.count(),
                             "sources": _store.sources()})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            body = self._read_json()
            route = self.path.rstrip("/")
            if route == "/index":
                self._send(200, _index(body.get("documents", [])))
            elif route == "/retrieve-context":
                self._send(200, _retrieve(body))
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001 — never crash the server on a bad request
            self._send(500, {"error": str(e)})

    def log_message(self, *args):  # keep stdout quiet; launchd logs stderr
        pass


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"RAG service ready on http://{HOST}:{PORT} "
          f"(model={DEFAULT_MODEL}, chunks={_store.count()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
