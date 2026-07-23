#!/usr/bin/env python3
"""
index_docs.py — push documents into the running RAG service.

Reads a JSON file (or stdin) containing a list of documents and POSTs them to
the service's /index endpoint. Used by the Confluence ingestion routine: a
Claude session pulls pages via the Atlassian connector, writes them to a JSON
file, then calls this script.

Document shape:
  [{"source": "<page-id>", "title": "...", "url": "...", "content": "..."}]

Usage:
  python3 rag/index_docs.py docs.json
  cat docs.json | python3 rag/index_docs.py -
  RAG_URL=http://localhost:8001 python3 rag/index_docs.py docs.json
"""

import json
import os
import sys
import urllib.request

RAG_URL = os.environ.get("RAG_URL", "http://localhost:8000")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: index_docs.py <docs.json|->", file=sys.stderr)
        return 2

    raw = sys.stdin.read() if argv[1] == "-" else open(argv[1], encoding="utf-8").read()
    documents = json.loads(raw)
    if not isinstance(documents, list):
        print("error: expected a JSON list of documents", file=sys.stderr)
        return 2

    payload = json.dumps({"documents": documents}).encode("utf-8")
    req = urllib.request.Request(
        RAG_URL.rstrip("/") + "/index",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
