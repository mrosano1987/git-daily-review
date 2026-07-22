"""
kb_updater.py — Smart Knowledge Base Updater for Git Daily Review.

After each review, this module:
  1. Asks the AI: "what new knowledge did today's review surface?"
  2. Collects architectural context from the RAG service (if available)
  3. Stores structured suggestions in config/kb_suggestions/
  4. Auto-merges low-risk suggestions (info/warning level additions)
  5. Queues high-impact suggestions for human approval

Suggestion lifecycle:
  pending  → approved (auto or manual) → merged into knowledge-base.yaml
           → rejected → archived

Each suggestion tracks:
  - source: "ai_review" | "rag" | "manual"
  - layer:  L0-L6
  - type:   add_rule | add_gate | add_domain_rule | add_integration | update_metadata
  - confidence: 0.0-1.0
  - auto_approvable: bool (True for info/warning additions)
"""

import json
import os
import re
import yaml
from datetime import datetime
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


ROOT_DIR = Path(__file__).parent.parent
KB_PATH       = ROOT_DIR / "config" / "knowledge-base.yaml"
SUGGESTIONS_DIR = ROOT_DIR / "config" / "kb_suggestions"

# ── Auto-merge policy (enforced by CODE, never delegated to the LLM) ────────
# The model may *propose* auto_approvable=true, but the final decision is:
#   model_flag AND _policy_allows_auto(...)
AUTO_MERGE_MIN_CONFIDENCE = 0.85
AUTO_MERGE_MAX_PER_RUN    = 2   # hard cap: prevents KB pollution over time


def _policy_allows_auto(stype: str, section: str, content: dict) -> bool:
    """
    Code-level whitelist of what may be merged without human review:
      - add_rule into conventions.*      (style/convention hints)
      - add_gate with severity info/warnings only
    Everything else (critical/error gates, domain rules, integrations,
    metadata edits) always requires human approval.
    """
    if stype == "add_rule":
        return section.startswith("conventions.")
    if stype == "add_gate":
        return content.get("severity", "") in ("info", "warnings", "warning")
    return False


# ── Data model ──────────────────────────────────────────────────────────────

@dataclass
class KBSuggestion:
    id:              str
    date:            str
    source:          str       # "ai_review" | "rag" | "manual"
    layer:           str       # "L0"-"L6"
    section:         str       # e.g. "quality_gates.warnings", "conventions.angular"
    type:            str       # "add_rule" | "add_gate" | "add_domain_rule" | "add_integration" | "update_metadata"
    content:         dict
    reason:          str
    confidence:      float     # 0.0 - 1.0
    auto_approvable: bool      # True = safe to merge without human review
    status:          str = "pending"   # pending | approved | rejected | merged
    repo:            str = ""
    reviewed_at:     Optional[str] = None
    merged_at:       Optional[str] = None


def _suggestion_id(date: str, idx: int) -> str:
    # Componente oraria: evita collisioni/overwrite anche se file vengono
    # cancellati dalla cartella (il vecchio schema contava i file esistenti).
    time_part = datetime.now().strftime("%H%M%S")
    return f"{date.replace('-', '')}_{time_part}_{idx:02d}"


# ── Extraction prompt ────────────────────────────────────────────────────────

def _build_extraction_prompt(repo_reviews: list, current_kb_summary: str,
                              rag_context: str = "") -> str:
    """Build the prompt that asks the AI to extract KB-worthy learnings."""

    findings_summary = []
    for review in repo_reviews:
        if not review.commit_reviews:
            continue
        findings_summary.append(f"\n### Repository: {review.repo_name}")
        if review.overall_summary:
            findings_summary.append(f"Summary: {review.overall_summary}")

        all_findings = [f for cr in review.commit_reviews for f in cr.findings]
        for f in all_findings:
            findings_summary.append(
                f"- [{f.severity.upper()}] {f.rule_id}: {f.message}"
                + (f" → {f.suggestion}" if f.suggestion else "")
            )

        if review.critical_issues:
            for issue in review.critical_issues:
                findings_summary.append(f"- [CRITICAL] {issue}")

    if not findings_summary:
        return ""

    rag_block = ""
    if rag_context:
        rag_block = f"""
## Context from RAG service
{rag_context}
"""

    return f"""You are a knowledge base curator for a software project.

Below are the findings from today's automated code review, followed by a summary
of the current knowledge base and optional RAG context.

Your task: identify NEW knowledge worth adding to the knowledge base — rules, patterns,
domain constraints, integration details, or release notes that were surfaced by today's
review but are NOT yet documented.

## Today's review findings
{"".join(findings_summary)}
{rag_block}
## Current knowledge base (summary)
{current_kb_summary}

## Instructions

Respond with a JSON array of suggestions. Each suggestion:

{{
  "layer": "L2",
  "section": "conventions.angular",
  "type": "add_rule",
  "content": {{
    "rule": "The exact rule text to add"
  }},
  "reason": "Why this should be added (1-2 sentences)",
  "confidence": 0.85,
  "auto_approvable": true
}}

Types:
  - add_rule          → new item in a conventions or quality_gates list
  - add_gate          → new quality gate (with id, name, description, severity)
  - add_domain_rule   → new business rule in domain.business_rules
  - add_integration   → new entry in integrations
  - update_metadata   → update metadata.changelog or similar

Layers: L0 (identity), L1 (architecture), L2 (conventions), L3 (quality_gates),
        L4 (domain), L5 (integrations), L6 (release)

auto_approvable rules:
  - true  → safe additions: new info/warning gates, new conventions, new hints
  - false → requires human review: critical/error gates, domain rules, integrations,
            anything modifying existing rules

Only suggest things genuinely NEW and useful. Skip obvious or already-documented items.
If nothing new was learned, return an empty array: []

Respond ONLY with the JSON array, no other text.
"""


def _summarize_kb(kb: dict) -> str:
    """Create a concise text summary of the current KB for the extraction prompt."""
    lines = []

    proj = kb.get("project", {})
    if proj.get("name"):
        lines.append(f"Project: {proj['name']}")

    conv = kb.get("conventions", {})
    for section, data in conv.items():
        if isinstance(data, dict) and data.get("rules"):
            lines.append(f"Conventions/{section}: {len(data['rules'])} rules")

    gates = kb.get("quality_gates", {})
    for severity, items in gates.items():
        if isinstance(items, list) and items:
            ids = [g.get("id", "?") for g in items]
            lines.append(f"Gates/{severity}: {', '.join(ids)}")

    domain = kb.get("domain", {})
    for area in domain.get("business_rules", []):
        lines.append(f"Domain/{area.get('area', '?')}: {len(area.get('rules', []))} rules")

    integrations = kb.get("integrations", [])
    if integrations:
        names = [i.get("name", "?") for i in integrations]
        lines.append(f"Integrations: {', '.join(names)}")

    metadata = kb.get("metadata", {})
    if metadata.get("version"):
        lines.append(f"KB version: {metadata['version']}, last updated: {metadata.get('last_updated','?')}")

    return "\n".join(lines) if lines else "Knowledge base is empty."


# ── RAG-driven extraction ────────────────────────────────────────────────────

def _extract_from_rag_context(rag_contexts: list, current_kb: dict) -> list[KBSuggestion]:
    """
    Parse RAG context chunks for architectural/integration knowledge
    not yet in the KB and generate suggestions.

    This is a lightweight heuristic pass — no extra LLM call needed.
    """
    suggestions = []
    today = datetime.now().strftime("%Y-%m-%d")
    known_integrations = {
        i.get("name", "").lower()
        for i in current_kb.get("integrations", [])
    }

    for ctx in rag_contexts:
        content = ctx.get("content", "")
        source  = ctx.get("source", "")
        score   = ctx.get("score", 0.0)

        if score < 0.75 or not content:
            continue

        # Detect integration mentions not in KB
        integration_patterns = [
            r"(?:API|service|endpoint|integration)[\s:]+([A-Z][a-zA-Z0-9\s]+?)(?:\s+API|\s+service|\s+integration)",
            r"calls?\s+([A-Z][a-zA-Z0-9]+(?:Service|API|Gateway|Client))",
        ]
        for pattern in integration_patterns:
            for match in re.finditer(pattern, content):
                name = match.group(1).strip()
                if name.lower() not in known_integrations and len(name) > 3:
                    suggestions.append(KBSuggestion(
                        id="",
                        date=today,
                        source="rag",
                        layer="L5",
                        section="integrations",
                        type="add_integration",
                        content={
                            "name": name,
                            "type": "Unknown — verify",
                            "description": f"Detected in RAG context chunk '{source}'",
                            "conventions": ["Verify and document integration details"],
                        },
                        reason=f"Integration '{name}' appears in codebase (RAG: {source}) but is not documented in KB",
                        confidence=0.5,
                        auto_approvable=False,
                    ))
                    known_integrations.add(name.lower())

    return suggestions


# ── AI-driven extraction ─────────────────────────────────────────────────────

def extract_learnings(repo_reviews: list, config_dir: str,
                      ai_config: dict,
                      rag_contexts: list = None) -> list[KBSuggestion]:
    """
    Run the learning extraction pipeline:
    1. Heuristic RAG analysis
    2. AI-driven review analysis

    Returns a list of KBSuggestion objects (not yet saved or merged).
    """
    suggestions = []
    today = datetime.now().strftime("%Y-%m-%d")
    kb = _load_kb()

    # RAG pass (fast, no LLM call)
    if rag_contexts:
        rag_suggestions = _extract_from_rag_context(rag_contexts, kb)
        suggestions.extend(rag_suggestions)

    # AI pass
    rag_context_text = ""
    if rag_contexts:
        rag_context_text = "\n---\n".join(
            f"[{c.get('source','?')}]: {c.get('content','')[:500]}"
            for c in (rag_contexts or [])[:5]
            if c.get("score", 0) > 0.7
        )

    prompt = _build_extraction_prompt(
        repo_reviews, _summarize_kb(kb), rag_context_text
    )
    if not prompt:
        return suggestions

    try:
        sys_path_insert = str(Path(__file__).parent)
        import sys
        if sys_path_insert not in sys.path:
            sys.path.insert(0, sys_path_insert)
        from ai_provider import create_provider

        provider = create_provider(ai_config)
        response_text = provider.complete(prompt)

        # Parse JSON
        text = response_text.strip()
        if "```" in text:
            text = re.sub(r"```(?:json)?", "", text).strip().strip("`")
        first = text.find("[")
        last  = text.rfind("]")
        if first != -1 and last > first:
            text = text[first:last + 1]

        raw_suggestions = json.loads(text)

        for i, raw in enumerate(raw_suggestions):
            if not isinstance(raw, dict):
                continue
            stype      = raw.get("type", "add_rule")
            section    = raw.get("section", "")
            content    = raw.get("content", {})
            confidence = float(raw.get("confidence", 0.5))
            # Il flag del modello è solo una PROPOSTA: la decisione finale
            # è del codice (whitelist + soglia di confidenza).
            model_flag = bool(raw.get("auto_approvable", False))
            auto_ok = (
                model_flag
                and confidence >= AUTO_MERGE_MIN_CONFIDENCE
                and _policy_allows_auto(stype, section, content)
            )
            suggestions.append(KBSuggestion(
                id="",
                date=today,
                source="ai_review",
                layer=raw.get("layer", "L2"),
                section=section,
                type=stype,
                content=content,
                reason=raw.get("reason", ""),
                confidence=confidence,
                auto_approvable=auto_ok,
            ))

    except Exception as e:
        # Learning extraction is best-effort — never block the main review
        print(f"    ⚠️  KB learning extraction failed: {e}")

    # Assign IDs
    offset = len(list(SUGGESTIONS_DIR.glob("*.yaml"))) if SUGGESTIONS_DIR.exists() else 0
    for i, s in enumerate(suggestions):
        s.id = _suggestion_id(today, offset + i)

    return suggestions


# ── Persistence ──────────────────────────────────────────────────────────────

def save_suggestions(suggestions: list[KBSuggestion]) -> list[str]:
    """Save suggestions to YAML files in config/kb_suggestions/."""
    SUGGESTIONS_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for s in suggestions:
        path = SUGGESTIONS_DIR / f"{s.id}.yaml"
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(asdict(s), f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)
        saved.append(str(path))
    return saved


def load_pending_suggestions() -> list[KBSuggestion]:
    """Load all pending suggestions from disk."""
    if not SUGGESTIONS_DIR.exists():
        return []
    suggestions = []
    for path in sorted(SUGGESTIONS_DIR.glob("*.yaml")):
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data.get("status") == "pending":
                suggestions.append(KBSuggestion(**data))
        except Exception:
            continue
    return suggestions


def _update_suggestion_status(suggestion_id: str, status: str):
    path = SUGGESTIONS_DIR / f"{suggestion_id}.yaml"
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data["status"] = status
    if status in ("approved", "rejected"):
        data["reviewed_at"] = datetime.now().isoformat()
    if status == "merged":
        data["merged_at"] = datetime.now().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── KB merging ───────────────────────────────────────────────────────────────

def _load_kb() -> dict:
    if not KB_PATH.exists():
        return {}
    with open(KB_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_kb(kb: dict):
    with open(KB_PATH, "w", encoding="utf-8") as f:
        yaml.dump(kb, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False, width=120)


def _merge_suggestion(kb: dict, suggestion: KBSuggestion) -> bool:
    """
    Apply a single suggestion to the KB dict in-place.
    Returns True if the merge was successful.
    """
    try:
        stype   = suggestion.type
        section = suggestion.section
        content = suggestion.content

        if stype == "add_rule":
            # Navigate to section: e.g. "conventions.angular" → kb["conventions"]["angular"]["rules"]
            parts = section.split(".")
            node = kb
            for part in parts:
                node = node.setdefault(part, {})
            rules = node.setdefault("rules", [])
            rule_text = content.get("rule", "")
            if rule_text and rule_text not in rules:
                rules.append(rule_text)
            else:
                return False  # already present

        elif stype == "add_gate":
            severity = content.get("severity", "warnings")
            gate_list = kb.setdefault("quality_gates", {}).setdefault(severity, [])
            new_id = content.get("id", "")
            if new_id and any(g.get("id") == new_id for g in gate_list):
                return False  # duplicate
            gate = {k: v for k, v in content.items() if k != "severity"}
            gate_list.append(gate)

        elif stype == "add_domain_rule":
            area_name = content.get("area", "")
            rule_text = content.get("rule", "")
            business_rules = kb.setdefault("domain", {}).setdefault("business_rules", [])
            for area in business_rules:
                if area.get("area") == area_name:
                    if rule_text not in area.setdefault("rules", []):
                        area["rules"].append(rule_text)
                    return True
            # New area
            business_rules.append({"area": area_name, "rules": [rule_text]})

        elif stype == "add_integration":
            integrations = kb.setdefault("integrations", [])
            name = content.get("name", "")
            if any(i.get("name") == name for i in integrations):
                return False
            integrations.append(content)

        elif stype == "update_metadata":
            pass  # handled by _update_kb_metadata

        else:
            return False

        return True

    except Exception as e:
        print(f"    ⚠️  Failed to merge suggestion {suggestion.id}: {e}")
        return False


def _update_kb_metadata(kb: dict, merged_count: int, today: str):
    """Bump KB version and append changelog entry."""
    meta = kb.setdefault("metadata", {})

    # Bump patch version
    version = meta.get("version", "1.0.0")
    parts = version.split(".")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except (ValueError, IndexError):
        parts = ["1", "0", "1"]
    meta["version"] = ".".join(parts)
    meta["last_updated"] = today
    meta.setdefault("auto_update_source", []).append({
        "date": today,
        "suggestions_merged": merged_count,
    })

    changelog = meta.setdefault("changelog", [])
    changelog.append({
        "date": today,
        "change": f"Auto-updated: {merged_count} suggestion(s) merged from daily review",
    })


def apply_suggestions(suggestions: list[KBSuggestion],
                      auto_only: bool = True) -> dict:
    """
    Apply suggestions to the knowledge base YAML.

    Args:
        suggestions:  list of KBSuggestion objects (pending)
        auto_only:    if True, only merge auto_approvable suggestions

    Returns a summary dict with counts.
    """
    if not suggestions:
        return {"merged": 0, "queued": 0, "skipped": 0}

    to_merge = [s for s in suggestions
                if not auto_only or s.auto_approvable]
    to_queue = [s for s in suggestions
                if auto_only and not s.auto_approvable]

    # Tetto per run: le eccedenze finiscono in coda di revisione umana,
    # così la KB non si riempie di rumore anche con un modello "generoso".
    if auto_only and len(to_merge) > AUTO_MERGE_MAX_PER_RUN:
        to_merge.sort(key=lambda s: s.confidence, reverse=True)
        overflow = to_merge[AUTO_MERGE_MAX_PER_RUN:]
        to_merge = to_merge[:AUTO_MERGE_MAX_PER_RUN]
        for s in overflow:
            s.auto_approvable = False
        to_queue.extend(overflow)

    kb = _load_kb()
    merged = 0
    skipped = 0
    today = datetime.now().strftime("%Y-%m-%d")

    for s in to_merge:
        if _merge_suggestion(kb, s):
            merged += 1
            _update_suggestion_status(s.id, "merged")
            s.status = "merged"
        else:
            skipped += 1
            _update_suggestion_status(s.id, "merged")  # already present

    if merged > 0:
        _update_kb_metadata(kb, merged, today)
        _save_kb(kb)

    # Save pending (human review needed)
    for s in to_queue:
        _update_suggestion_status(s.id, "pending")

    return {
        "merged": merged,
        "queued": len(to_queue),
        "skipped": skipped,
    }


# ── Report section ───────────────────────────────────────────────────────────

def format_suggestions_for_report(suggestions: list[KBSuggestion],
                                   apply_result: dict) -> str:
    """
    Format the KB update section for inclusion in the daily report.
    """
    if not suggestions:
        return ""

    lines = [
        "",
        "---",
        "",
        "## 📚 Knowledge Base Updates",
        "",
    ]

    merged  = [s for s in suggestions if s.status == "merged"]
    pending = [s for s in suggestions if s.status == "pending"]

    if merged:
        lines.append(f"### ✅ Auto-merged ({len(merged)})")
        lines.append("")
        for s in merged:
            layer_label = s.layer
            lines.append(f"- **[{layer_label} / {s.section}]** {s.reason}")
            content_preview = _content_preview(s)
            if content_preview:
                lines.append(f"  > {content_preview}")
        lines.append("")

    if pending:
        lines.append(f"### ⏳ Pending review ({len(pending)})")
        lines.append("")
        for s in pending:
            icon = "🔴" if not s.auto_approvable else "🟡"
            lines.append(
                f"- {icon} **[{s.layer} / {s.section}]** {s.reason} "
                f"*(confidence: {s.confidence:.0%}, source: {s.source})*"
            )
        lines.append("")
        lines.append(
            "_Review pending suggestions: "
            "`python scripts/kb_manager.py --review`_"
        )
        lines.append("")

    return "\n".join(lines)


def _content_preview(s: KBSuggestion) -> str:
    c = s.content
    if s.type == "add_rule":
        return c.get("rule", "")[:120]
    if s.type == "add_gate":
        return f"[{c.get('severity','?').upper()}] {c.get('id','?')}: {c.get('name','')}"
    if s.type == "add_domain_rule":
        return f"{c.get('area','?')}: {c.get('rule','')[:80]}"
    if s.type == "add_integration":
        return f"{c.get('name','?')} ({c.get('type','?')})"
    return ""