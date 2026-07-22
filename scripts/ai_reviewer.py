"""
ai_reviewer.py — Analisi dei commit tramite Anthropic API

Usa la libreria `anthropic` con la API key da ANTHROPIC_API_KEY.
Richiede: pip install anthropic pyyaml
"""

import os
import json
import yaml
from pathlib import Path
from dataclasses import dataclass, field

from ai_provider import create_provider, PROVIDER_PRESETS

# RAG client — optional, degrades silently if not available
try:
    from rag_client import retrieve_context, format_context_for_prompt
    _RAG_MODULE_AVAILABLE = True
except ImportError:
    _RAG_MODULE_AVAILABLE = False


@dataclass
class ReviewFinding:
    rule_id: str
    severity: str       # critical, error, warning, info
    file: str
    line: str
    message: str
    suggestion: str = ""


@dataclass
class CommitReview:
    commit_hash: str
    commit_message: str
    author: str
    branch: str
    summary: str = ""
    findings: list = field(default_factory=list)
    quality_score: int = 0  # 1-10


@dataclass
class RepoReview:
    repo_name: str
    date: str
    commit_reviews: list = field(default_factory=list)
    overall_summary: str = ""
    critical_issues: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def load_knowledge_base(config_dir: str) -> str:
    """
    Carica la knowledge base del progetto e la converte in testo
    strutturato per il prompt dell'AI reviewer.

    Cerca in ordine:
    1. knowledge-base.yaml (generata dal wizard, aggiornata da kb_updater)
    2. project-knowledge-base.yaml (nome legacy)
    3. project-patterns.yaml (fallback legacy)
    """
    kb_path = Path(config_dir) / "knowledge-base.yaml"
    legacy_kb_path = Path(config_dir) / "project-knowledge-base.yaml"
    legacy_path = Path(config_dir) / "project-patterns.yaml"

    if kb_path.exists():
        return _render_knowledge_base(kb_path)
    elif legacy_kb_path.exists():
        return _render_knowledge_base(legacy_kb_path)
    elif legacy_path.exists():
        return _render_legacy_patterns(legacy_path)
    else:
        return "Nessuna knowledge base trovata."


def _render_knowledge_base(kb_path: Path) -> str:
    """Converte la knowledge base YAML in testo strutturato per il prompt."""
    with open(kb_path, "r", encoding="utf-8") as f:
        kb = yaml.safe_load(f)

    sections = []

    # ── L0: Identità ──
    project = kb.get("project", {})
    if project:
        sections.append("## CONTESTO PROGETTO")
        sections.append(f"Nome: {project.get('name', 'N/A')}")
        if project.get("description"):
            sections.append(f"Descrizione: {project['description'].strip()}")
        partners = project.get("team", {}).get("partners", [])
        if partners:
            partners_str = ", ".join(f"{p['name']} ({p['role']})" for p in partners)
            sections.append(f"Partner: {partners_str}")
        waves = project.get("development_waves", [])
        if waves:
            waves_str = ", ".join(f"{w['id']}: {w['name']} [{w.get('status','')}]" for w in waves)
            sections.append(f"Wave di sviluppo: {waves_str}")
        sections.append("")

    # ── L1: Architettura ──
    arch = kb.get("architecture", {})
    if arch:
        sections.append("## ARCHITETTURA")
        for repo_key, repo in arch.get("repositories", {}).items():
            ts = repo.get("tech_stack", {})
            sections.append(f"\n### Repository: {repo.get('name', repo_key)}")
            if repo.get("description"):
                sections.append(f"  {repo['description']}")
            if ts:
                # Supporta sia il formato wizard (liste) sia quello legacy (scalari)
                langs = ts.get("languages") or ([ts["language"]] if ts.get("language") else [])
                fws = ts.get("frameworks") or ([ts["framework"]] if ts.get("framework") else [])
                tools = ts.get("tools") or []
                stack_parts = list(dict.fromkeys(fws)) + list(dict.fromkeys(langs)) + list(tools)
                if ts.get("mobile"):
                    stack_parts.append(ts["mobile"])
                if stack_parts:
                    sections.append(f"  Stack: {' / '.join(stack_parts)}")
            dirs = repo.get("structure", {}).get("key_directories", [])
            for d in dirs:
                sections.append(f"  - `{d['path']}` → {d['purpose']}")

        patterns = arch.get("patterns", [])
        if patterns:
            sections.append("\n### Pattern architetturali")
            for p in patterns:
                sections.append(f"  - **{p['name']}**: {p['description'].strip()}")
        sections.append("")

    # ── L2: Convenzioni ──
    conv = kb.get("conventions", {})
    if conv:
        sections.append("## CONVENZIONI DI CODICE")
        for category, data in conv.items():
            if category == "documents":
                continue
            if isinstance(data, dict):
                rules = data.get("rules", [])
                if rules:
                    sections.append(f"\n### {category.replace('_', ' ').title()}")
                    for rule in rules:
                        if isinstance(rule, str):
                            sections.append(f"  - {rule}")
                        elif isinstance(rule, dict):
                            sections.append(f"  - `{rule.get('pattern', '')}` → {rule.get('for', '')}")
        sections.append("")

    # ── L3: Quality Gates ──
    gates = kb.get("quality_gates", {})
    if gates:
        sections.append("## QUALITY GATES (regole da verificare)")
        for severity_level in ("critical", "errors", "warnings", "info"):
            gate_list = gates.get(severity_level, [])
            if not gate_list:
                continue
            label = severity_level.upper()
            sections.append(f"\n### [{label}]")
            for g in gate_list:
                gid = g.get("id", "")
                name = g.get("name", "")
                desc = g.get("description", "") or g.get("desc", "")
                scope = g.get("scope", [])
                suggestion = g.get("suggestion", "")
                heuristic = g.get("heuristic", "")

                entry = f"  - **{gid}** — {name}: {desc}"
                if scope:
                    entry += f" [repo: {', '.join(scope)}]"
                sections.append(entry)

                if heuristic:
                    sections.append(f"    Come verificare: {heuristic.strip()}")
                if suggestion:
                    sections.append(f"    Suggerimento: {suggestion}")
        sections.append("")

    # ── L4: Dominio ──
    domain = kb.get("domain", {})
    if domain:
        sections.append("## REGOLE DI DOMINIO (business)")
        if domain.get("description"):
            sections.append(f"  {domain['description'].strip()}")
        for area in domain.get("business_rules", []):
            sections.append(f"\n### {area.get('area', '')}")
            for rule in area.get("rules", []):
                sections.append(f"  - {rule}")
        sections.append("")

    # ── L5: Integrazioni ──
    integrations = kb.get("integrations", [])
    if integrations:
        sections.append("## CONTRATTI DI INTEGRAZIONE")
        for integ in integrations:
            sections.append(f"\n### {integ.get('name', '')} ({integ.get('type', '')})")
            sections.append(f"  URL config key: `{integ.get('base_url_key', '')}`")
            if integ.get("auth"):
                sections.append(f"  Auth: {integ['auth']}")
            for conv in integ.get("conventions", []):
                sections.append(f"  - {conv}")
        sections.append("")

    # ── L6: Rilascio ──
    release = kb.get("release", {})
    if release:
        sections.append("## REGOLE DI RILASCIO")
        if release.get("cadence"):
            sections.append(f"  Cadenza: {release['cadence']}")
        for platform, pdata in release.get("platforms", {}).items():
            if isinstance(pdata, dict) and pdata.get("rules"):
                sections.append(f"\n### {platform.upper()}")
                for rule in pdata["rules"]:
                    sections.append(f"  - {rule}")
        sections.append("")

    return "\n".join(sections)


def _render_legacy_patterns(patterns_path: Path) -> str:
    """Fallback: carica il vecchio project-patterns.yaml."""
    with open(patterns_path, "r", encoding="utf-8") as f:
        patterns = yaml.safe_load(f)

    lines = ["## Pattern e Convenzioni del Progetto\n"]
    for category, data in patterns.items():
        if category.startswith("#") or not isinstance(data, dict):
            continue
        rules = data.get("rules", [])
        if not rules:
            continue
        lines.append(f"\n### {category.upper()}")
        for rule in rules:
            sev = rule.get("severity", "info")
            rid = rule.get("id", "")
            name = rule.get("name", "")
            desc = rule.get("description", "")
            lines.append(f"- [{sev.upper()}] {rid}: {name} — {desc}")

    return "\n".join(lines)


def build_review_prompt(commits_data: list[dict], knowledge_text: str,
                        repo_name: str,
                        rag_context_text: str = "") -> str:
    """Costruisce il prompt per la review."""
    commits_section = []
    for c in commits_data:
        section = f"""
--- COMMIT {c['short_hash']} ---
Branch: {c['branch']}
Autore: {c['author']}
Data: {c['date']}
Messaggio: {c['message']}
File modificati ({c['files_changed']}): +{c['insertions']} -{c['deletions']}
File: {', '.join(c.get('files', [])[:20])}
"""
        if c.get("diff"):
            section += f"\nDIFF:\n```\n{c['diff'][:6000]}\n```"
        commits_section.append(section)

    # RAG context block — injected only when available
    rag_block = ""
    if rag_context_text:
        rag_block = f"""
{rag_context_text}

---
"""

    prompt = f"""Sei un senior code reviewer per il progetto descritto sotto.
Stai analizzando i commit del giorno sul repository **{repo_name}**.

Usa il contesto del progetto per produrre una review precisa e specifica,
non generica. Verifica in particolare i QUALITY GATES elencati e le REGOLE
DI DOMINIO se i file modificati toccano quelle aree.
{rag_block}
{knowledge_text}

---

## Commit da analizzare

{"".join(commits_section)}

---

## Istruzioni

Produci un'analisi in formato JSON con questa struttura:

{{
  "overall_summary": "Riepilogo generale delle attività del giorno in 2-3 frasi",
  "commit_reviews": [
    {{
      "commit_hash": "abc1234",
      "summary": "Breve descrizione di cosa fa il commit",
      "quality_score": 8,
      "findings": [
        {{
          "rule_id": "TS-001",
          "severity": "critical|error|warning|info",
          "file": "path/del/file.ts",
          "line": "contesto o numero riga",
          "message": "Descrizione del problema",
          "suggestion": "Come risolvere"
        }}
      ]
    }}
  ],
  "critical_issues": ["Lista problemi critici che richiedono attenzione immediata"],
  "positive_notes": ["Cose fatte bene, pattern corretti"]
}}

Regole:
- quality_score: 1-10 (10 = perfetto)
- Segnala GIT-001 se il messaggio di commit è generico (solo "fix", "update", "wip")
- findings vuoto se non ci sono problemi per quel commit
- Rispondi SOLO con JSON valido, senza backtick, senza testo aggiuntivo
"""
    return prompt


def parse_json_response(text: str) -> dict:
    """Estrae e parsa il JSON dalla risposta, gestendo eventuali backtick."""
    text = text.strip()

    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start) if "```" in text[start:] else len(text)
        text = text[start:end].strip()
    elif text.startswith("```"):
        start = 3
        if text[start:start + 4] == "json":
            start += 4
        end = text.index("```", start) if "```" in text[start:] else len(text)
        text = text[start:end].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback: cerca il primo { e l'ultimo }
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        return json.loads(text[first:last + 1])

    raise json.JSONDecodeError("Nessun JSON valido trovato", text, 0)


def review_commits(commits_data: list[dict], repo_name: str,
                   config_dir: str, config: dict) -> RepoReview:
    """
    Esegue la review dei commit tramite Anthropic API.

    Args:
        commits_data : lista di commit serializzati (da git_collector)
        repo_name    : nome del repository
        config_dir   : path alla cartella config/ (contiene knowledge base)
        config       : sezione 'ai' di config.yaml
    """
    review = RepoReview(
        repo_name=repo_name,
        date=commits_data[0]["date"][:10] if commits_data else ""
    )

    if not commits_data:
        review.overall_summary = "Nessun commit da analizzare."
        return review

    # Batch se troppi commit
    if len(commits_data) > 8:
        return _review_in_batches(commits_data, repo_name, config_dir, config)

    knowledge_text = load_knowledge_base(config_dir)

    # ── RAG enrichment (optional) ──
    rag_context_text = ""
    rag_url = config.get("rag_url", "")
    if _RAG_MODULE_AVAILABLE and rag_url:
        # Collect all changed files across the batch
        all_files = []
        full_diff = []
        for c in commits_data:
            all_files.extend(c.get("files", []))
            if c.get("diff"):
                full_diff.append(c["diff"])

        # Use branch from first commit (best effort)
        branch = commits_data[0].get("branch", "") if commits_data else ""

        rag_ctx = retrieve_context(
            base_url=rag_url,
            repo_name=repo_name,
            branch=branch,
            changed_files=list(dict.fromkeys(all_files)),  # dedup, preserve order
            diff="\n---\n".join(full_diff),
        )
        rag_context_text = format_context_for_prompt(rag_ctx)

        if rag_ctx.available:
            print(f"    🔍 RAG context: {len(rag_ctx.contexts)} chunks"
                  f" | risk: {rag_ctx.risk_score}")

    prompt = build_review_prompt(commits_data, knowledge_text, repo_name, rag_context_text)

    # ── LLM call via configurable provider ──
    try:
        provider = create_provider(config)
        print(f"    🤖 {provider.name} / {provider.model}")
        response_text = provider.complete(prompt)
        result = parse_json_response(response_text)

        review.overall_summary = result.get("overall_summary", "")
        review.critical_issues = result.get("critical_issues", [])

        for cr in result.get("commit_reviews", []):
            commit_review = CommitReview(
                commit_hash=cr.get("commit_hash", ""),
                commit_message="",
                author="",
                branch="",
                summary=cr.get("summary", ""),
                quality_score=cr.get("quality_score", 0),
                findings=[
                    ReviewFinding(
                        rule_id=f.get("rule_id", ""),
                        severity=f.get("severity", "info"),
                        file=f.get("file", ""),
                        line=f.get("line", ""),
                        message=f.get("message", ""),
                        suggestion=f.get("suggestion", "")
                    )
                    for f in cr.get("findings", [])
                ]
            )
            # Arricchisci con i dati git originali
            for c in commits_data:
                if (c["short_hash"] == cr.get("commit_hash") or
                        c["hash"].startswith(cr.get("commit_hash", "___"))):
                    commit_review.commit_message = c["message"]
                    commit_review.author = c["author"]
                    commit_review.branch = c["branch"]
                    break

            review.commit_reviews.append(commit_review)

        # Statistiche aggregate
        all_findings = [f for cr in review.commit_reviews for f in cr.findings]
        review.stats = {
            "total_commits_reviewed": len(review.commit_reviews),
            "avg_quality_score": (
                sum(cr.quality_score for cr in review.commit_reviews) /
                len(review.commit_reviews)
            ) if review.commit_reviews else 0,
            "findings_by_severity": {
                sev: sum(1 for f in all_findings if f.severity == sev)
                for sev in ("critical", "error", "warning", "info")
            },
            "positive_notes": result.get("positive_notes", [])
        }

    except json.JSONDecodeError as e:
        msg = f"AI response could not be parsed as JSON: {e}"
        review.overall_summary = msg
        review.critical_issues.append(msg)
    except RuntimeError as e:
        # Provider-level errors (auth, connection, rate limit)
        msg = str(e)
        review.overall_summary = msg
        review.critical_issues.append(msg)
    except Exception as e:
        msg = f"Unexpected error during review: {e}"
        review.overall_summary = msg
        review.critical_issues.append(msg)

    return review


def _review_in_batches(commits_data: list[dict], repo_name: str,
                       config_dir: str, config: dict) -> RepoReview:
    """Review a batch per repository con molti commit."""
    BATCH_SIZE = 8
    combined = RepoReview(
        repo_name=repo_name,
        date=commits_data[0]["date"][:10] if commits_data else ""
    )
    summaries = []
    total_batches = (len(commits_data) + BATCH_SIZE - 1) // BATCH_SIZE

    for i in range(0, len(commits_data), BATCH_SIZE):
        batch = commits_data[i:i + BATCH_SIZE]
        batch_num = (i // BATCH_SIZE) + 1
        print(f"    📦 Batch {batch_num}/{total_batches} ({len(batch)} commit)...")

        result = review_commits(batch, repo_name, config_dir, config)
        combined.commit_reviews.extend(result.commit_reviews)
        if result.overall_summary:
            summaries.append(result.overall_summary)
        combined.critical_issues.extend(result.critical_issues)

        if result.stats:
            if not combined.stats:
                combined.stats = dict(result.stats)
            else:
                combined.stats["total_commits_reviewed"] += result.stats.get(
                    "total_commits_reviewed", 0)
                for sev in ("critical", "error", "warning", "info"):
                    combined.stats.setdefault("findings_by_severity", {})[sev] = (
                        combined.stats["findings_by_severity"].get(sev, 0) +
                        result.stats.get("findings_by_severity", {}).get(sev, 0)
                    )
                combined.stats.setdefault("positive_notes", []).extend(
                    result.stats.get("positive_notes", [])
                )

    combined.overall_summary = " ".join(summaries)
    if combined.commit_reviews:
        combined.stats["avg_quality_score"] = (
            sum(cr.quality_score for cr in combined.commit_reviews) /
            len(combined.commit_reviews)
        )
    return combined