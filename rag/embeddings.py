"""
embeddings.py — Ollama embedding client for the local RAG service.

Talks to a local Ollama instance (the same one the review engine uses) and
returns dense vectors. No third-party deps: plain urllib.

Default model is bge-m3 (multilingual, strong on Italian + technical text),
overridable via the RAG_EMBED_MODEL / RAG_OLLAMA_URL env vars.
"""

import json
import os
import urllib.request

DEFAULT_MODEL = os.environ.get("RAG_EMBED_MODEL", "bge-m3")
DEFAULT_OLLAMA_URL = os.environ.get("RAG_OLLAMA_URL", "http://localhost:11434")


def embed(text: str, model: str = DEFAULT_MODEL,
          base_url: str = DEFAULT_OLLAMA_URL, timeout: int = 60) -> list[float]:
    """Return the embedding vector for a single string."""
    url = base_url.rstrip("/") + "/api/embeddings"
    payload = {"model": model, "prompt": text}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    vec = body.get("embedding")
    if not vec:
        raise RuntimeError(f"Ollama returned no embedding (model={model})")
    return vec


def embed_batch(texts: list[str], model: str = DEFAULT_MODEL,
                base_url: str = DEFAULT_OLLAMA_URL) -> list[list[float]]:
    """Embed a list of strings sequentially (Ollama has no true batch API)."""
    return [embed(t, model=model, base_url=base_url) for t in texts]
