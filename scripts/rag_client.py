"""
rag_client.py — Optional RAG service client for Git Daily Review.

Treats the RAG system as a completely external black-box service.
If the service is not configured or not reachable, everything degrades
gracefully — the review pipeline continues without context enrichment.

Expected RAG service contract:

  POST /retrieve-context
  {
    "repo":          str,
    "branch":        str,
    "changed_files": list[str],
    "diff":          str,
    "query":         str
  }

  Response:
  {
    "contexts": [
      {
        "source":  str,      # e.g. "ADR-014", "AuthService.ts"
        "score":   float,    # 0.0 - 1.0
        "content": str
      }
    ],
    "dependencies":  list[str],   # related components/modules
    "risk_score":    str,         # "low" | "medium" | "high"
    "summary":       str          # optional human-readable summary
  }

  Health check:
  GET /health  →  200 OK
"""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RAGContext:
    """Enriched context returned by the RAG service."""
    contexts: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    risk_score: str = "unknown"
    summary: str = ""
    source: str = "rag"         # for traceability in reports
    available: bool = False     # False = RAG not used


# Singleton-style availability cache (avoid re-pinging on every commit)
_rag_available: Optional[bool] = None


def is_rag_available(base_url: str, timeout: int = 3) -> bool:
    """
    Check if the RAG service is reachable.
    Result is cached for the lifetime of the process.
    """
    global _rag_available
    if _rag_available is not None:
        return _rag_available

    try:
        url = base_url.rstrip("/") + "/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _rag_available = resp.status == 200
    except Exception:
        _rag_available = False

    return _rag_available


def retrieve_context(
    base_url: str,
    repo_name: str,
    branch: str,
    changed_files: list[str],
    diff: str,
    timeout: int = 30,
    max_diff_chars: int = 8000,
) -> RAGContext:
    """
    Call the RAG service to retrieve enriched context for a set of changes.

    Returns RAGContext with available=False if the service is unreachable
    or returns an error — caller should handle this as a no-op.
    """
    empty = RAGContext(available=False)

    if not base_url:
        return empty

    if not is_rag_available(base_url):
        return empty

    payload = {
        "repo":          repo_name,
        "branch":        branch,
        "changed_files": changed_files[:50],        # safety cap
        "diff":          diff[:max_diff_chars],
        "query": (
            f"Review changes in {repo_name}: "
            f"{', '.join(changed_files[:5])}"
            + (f" and {len(changed_files) - 5} more" if len(changed_files) > 5 else "")
        ),
    }

    try:
        url = base_url.rstrip("/") + "/retrieve-context"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        return RAGContext(
            contexts=body.get("contexts", []),
            dependencies=body.get("dependencies", []),
            risk_score=body.get("risk_score", "unknown"),
            summary=body.get("summary", ""),
            source="rag",
            available=True,
        )

    except (urllib.error.URLError, json.JSONDecodeError, KeyError, Exception):
        # Any failure → silent degradation
        return RAGContext(available=False)


def format_context_for_prompt(rag_ctx: RAGContext, max_chars: int = 4000) -> str:
    """
    Convert RAGContext into a text block ready to be injected into the
    AI reviewer prompt. Returns empty string if RAG was not available.
    """
    if not rag_ctx.available or not rag_ctx.contexts:
        return ""

    lines = [
        "## Repository Context (from RAG service)",
        "",
    ]

    if rag_ctx.summary:
        lines += [f"**Summary:** {rag_ctx.summary}", ""]

    if rag_ctx.risk_score and rag_ctx.risk_score != "unknown":
        lines += [f"**Risk assessment:** {rag_ctx.risk_score}", ""]

    if rag_ctx.dependencies:
        deps = ", ".join(rag_ctx.dependencies[:10])
        lines += [f"**Related components:** {deps}", ""]

    lines.append("**Relevant context chunks:**")
    lines.append("")

    total_chars = 0
    for i, ctx in enumerate(rag_ctx.contexts):
        source  = ctx.get("source", f"source-{i}")
        score   = ctx.get("score", 0.0)
        content = ctx.get("content", "").strip()

        if not content:
            continue

        chunk = f"### [{source}] (relevance: {score:.2f})\n{content}\n"
        if total_chars + len(chunk) > max_chars:
            lines.append(f"_... {len(rag_ctx.contexts) - i} more context chunks truncated_")
            break

        lines.append(chunk)
        total_chars += len(chunk)

    return "\n".join(lines)