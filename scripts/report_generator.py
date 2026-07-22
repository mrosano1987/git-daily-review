"""
report_generator.py — Generazione del report giornaliero in Markdown

Combina i dati raccolti dal git_collector e le analisi dell'ai_reviewer
in un report .md strutturato e consultabile.
"""

import os
import json
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import asdict


SEVERITY_ICONS = {
    "critical": "🔴",
    "error": "🔴",
    "warning": "🟡",
    "info": "🔵",
}

QUALITY_LABELS = {
    range(1, 4): "❌ Scarso",
    range(4, 6): "⚠️ Sufficiente",
    range(6, 8): "✅ Buono",
    range(8, 10): "🌟 Ottimo",
    range(10, 11): "💎 Eccellente",
}


def quality_label(score: int) -> str:
    for r, label in QUALITY_LABELS.items():
        if score in r:
            return f"{score}/10 {label}"
    return f"{score}/10"


def generate_report(repo_reports: list, repo_reviews: list,
                    target_date: str, reports_dir: str,
                    trend_days: int = 7,
                    kb_section: str = "") -> str:
    """
    Genera il report giornaliero completo in formato Markdown.

    Args:
        repo_reports: lista di RepoReport (dati git)
        repo_reviews: lista di RepoReview (analisi AI)
        target_date: data in formato YYYY-MM-DD
        reports_dir: directory base dei report
        trend_days: giorni di storico per il trend
        kb_section: se presente, sezione KB da aggiungere al report

    Returns:
        Path del file report generato
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []

    # ── HEADER ──
    lines.append(f"# 📋 Daily Code Review — {target_date}")
    lines.append(f"")
    lines.append(f"> Generato il {now}")
    lines.append(f"> Range: {target_date} 00:00 → 23:59")
    lines.append(f"")

    # ── RIEPILOGO RAPIDO ──
    total_commits = sum(len(r.commits) for r in repo_reports)
    total_authors = set()
    total_files = 0
    total_insertions = 0
    total_deletions = 0

    for r in repo_reports:
        for c in r.commits:
            total_authors.add(c.author)
            total_files += c.files_changed
            total_insertions += c.insertions
            total_deletions += c.deletions

    lines.append(f"## 📊 Riepilogo Rapido")
    lines.append(f"")
    lines.append(f"| Metrica | Valore |")
    lines.append(f"|---------|--------|")
    lines.append(f"| Commit totali | **{total_commits}** |")
    lines.append(f"| Autori attivi | **{len(total_authors)}** ({', '.join(sorted(total_authors)) if total_authors else 'nessuno'}) |")
    lines.append(f"| File modificati | **{total_files}** |")
    lines.append(f"| Righe aggiunte | **+{total_insertions}** |")
    lines.append(f"| Righe rimosse | **-{total_deletions}** |")
    lines.append(f"")

    # Conteggio finding totali
    all_findings_count = {"critical": 0, "error": 0, "warning": 0, "info": 0}
    for review in repo_reviews:
        if hasattr(review, "stats") and review.stats:
            fbs = review.stats.get("findings_by_severity", {})
            for sev in all_findings_count:
                all_findings_count[sev] += fbs.get(sev, 0)

    if any(v > 0 for v in all_findings_count.values()):
        lines.append(f"### Segnalazioni")
        lines.append(f"")
        lines.append(f"| Severità | Conteggio |")
        lines.append(f"|----------|-----------|")
        for sev, count in all_findings_count.items():
            if count > 0:
                lines.append(f"| {SEVERITY_ICONS.get(sev, '⚪')} {sev.upper()} | {count} |")
        lines.append(f"")

    # ── ISSUE CRITICI ──
    all_critical = []
    for review in repo_reviews:
        if review.critical_issues:
            for issue in review.critical_issues:
                all_critical.append(f"[{review.repo_name}] {issue}")

    if all_critical:
        lines.append(f"## 🚨 Issue Critici")
        lines.append(f"")
        for issue in all_critical:
            lines.append(f"- {issue}")
        lines.append(f"")

    # ── DETTAGLIO PER REPOSITORY ──
    for repo_report, repo_review in zip(repo_reports, repo_reviews):
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## 📦 {repo_report.name}")
        lines.append(f"")

        if repo_report.errors:
            lines.append(f"⚠️ **Errori durante la raccolta:**")
            for err in repo_report.errors:
                lines.append(f"- {err}")
            lines.append(f"")

        if not repo_report.commits:
            lines.append(f"_Nessun commit per questa data._")
            lines.append(f"")
            continue

        # Summary AI
        if repo_review and repo_review.overall_summary:
            lines.append(f"### Riepilogo AI")
            lines.append(f"")
            lines.append(f"{repo_review.overall_summary}")
            lines.append(f"")

        # Positive notes
        if (repo_review and repo_review.stats and
                repo_review.stats.get("positive_notes")):
            lines.append(f"### ✅ Cose fatte bene")
            lines.append(f"")
            for note in repo_review.stats["positive_notes"]:
                lines.append(f"- {note}")
            lines.append(f"")

        # Commit per branch
        by_branch = {}
        for c in repo_report.commits:
            by_branch.setdefault(c.branch, []).append(c)

        lines.append(f"### Commit per Branch")
        lines.append(f"")

        for branch, commits in sorted(by_branch.items()):
            lines.append(f"#### 🌿 `{branch}` ({len(commits)} commit)")
            lines.append(f"")

            for c in commits:
                # Trova la review corrispondente
                cr = None
                if repo_review:
                    for r in repo_review.commit_reviews:
                        if r.commit_hash == c.short_hash or c.hash.startswith(r.commit_hash):
                            cr = r
                            break

                score_str = f" — {quality_label(cr.quality_score)}" if cr and cr.quality_score else ""
                lines.append(f"**`{c.short_hash}`** — {c.message}{score_str}")
                lines.append(f"  - 👤 {c.author} | 📅 {c.date[:19]}")
                lines.append(f"  - 📁 {c.files_changed} file | +{c.insertions} -{c.deletions}")

                if cr and cr.summary:
                    lines.append(f"  - 💡 _{cr.summary}_")

                # Findings per questo commit
                if cr and cr.findings:
                    lines.append(f"")
                    for f in cr.findings:
                        icon = SEVERITY_ICONS.get(f.severity, "⚪")
                        lines.append(f"  {icon} **[{f.rule_id}]** {f.message}")
                        if f.file:
                            lines.append(f"    - File: `{f.file}`")
                        if f.suggestion:
                            lines.append(f"    - 💬 {f.suggestion}")

                lines.append(f"")

    # ── TREND (se storico disponibile) ──
    trend_data = _load_trend(reports_dir, target_date, trend_days)
    if trend_data:
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"## 📈 Trend ultimi {trend_days} giorni")
        lines.append(f"")
        lines.append(f"| Data | Commit | File | +Ins | -Del | Quality avg |")
        lines.append(f"|------|--------|------|------|------|-------------|")
        for td in trend_data:
            lines.append(
                f"| {td['date']} | {td['commits']} | {td['files']} "
                f"| +{td['insertions']} | -{td['deletions']} "
                f"| {td.get('quality', 'n/a')} |"
            )
        lines.append(f"")

    if kb_section:
        lines.append(kb_section)
        lines.append(f"")

    # ── FOOTER ──
    lines.append(f"---")
    lines.append(f"")
    lines.append(f"_Report generato automaticamente dalla Daily Code Review Routine._")
    lines.append(f"_Per review interattiva: `claude \"Leggi reports/{target_date}/daily-summary.md\"`_")

    # Salva
    output_dir = os.path.join(reports_dir, target_date)
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "daily-summary.md")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return output_path


def _load_trend(reports_dir: str, target_date: str, days: int) -> list[dict]:
    """Carica i dati di trend dai report precedenti."""
    trend = []
    base_date = datetime.strptime(target_date, "%Y-%m-%d")

    for i in range(days, 0, -1):
        d = (base_date - timedelta(days=i)).strftime("%Y-%m-%d")
        day_dir = os.path.join(reports_dir, d)

        entry = {"date": d, "commits": 0, "files": 0,
                 "insertions": 0, "deletions": 0, "quality": "n/a"}

        # Cerca i JSON dei commit
        for repo_key in ["app", "apr"]:
            json_path = os.path.join(day_dir, f"{repo_key}-commits.json")
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r") as f:
                        data = json.load(f)
                    stats = data.get("stats", {})
                    entry["commits"] += stats.get("total_commits", 0)
                    entry["files"] += stats.get("total_files_changed", 0)
                    entry["insertions"] += stats.get("total_insertions", 0)
                    entry["deletions"] += stats.get("total_deletions", 0)
                except (json.JSONDecodeError, KeyError):
                    pass

        # Cerca il summary per il quality score
        summary_path = os.path.join(day_dir, "daily-summary.md")
        if os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                # Cerca quality scores nel report
                import re
                scores = re.findall(r"(\d+)/10", content)
                if scores:
                    avg = sum(int(s) for s in scores) / len(scores)
                    entry["quality"] = f"{avg:.1f}/10"
            except Exception:
                pass

        if entry["commits"] > 0 or os.path.exists(day_dir):
            trend.append(entry)

    return trend


def generate_history_index(reports_dir: str) -> str:
    """Genera un indice navigabile di tutti i report storici."""
    lines = ["# 📚 Storico Daily Code Reviews", ""]

    reports_path = Path(reports_dir)
    if not reports_path.exists():
        return "Nessun report trovato."

    # Trova tutte le directory YYYY-MM-DD
    report_dirs = sorted(
        [d for d in reports_path.iterdir()
         if d.is_dir() and len(d.name) == 10 and d.name[4] == "-"],
        reverse=True
    )

    if not report_dirs:
        return "Nessun report trovato."

    # Raggruppa per mese
    by_month = {}
    for d in report_dirs:
        month = d.name[:7]  # YYYY-MM
        by_month.setdefault(month, []).append(d)

    for month, dirs in by_month.items():
        lines.append(f"## {month}")
        lines.append("")

        for d in dirs:
            summary = d / "daily-summary.md"
            if summary.exists():
                # Leggi prima riga per info rapida
                with open(summary, "r", encoding="utf-8", errors="replace") as f:
                    first_lines = [f.readline() for _ in range(15)]
                # Cerca il conteggio commit
                commit_info = ""
                for line in first_lines:
                    if "Commit totali" in line:
                        commit_info = line.strip().split("|")[-2].strip() if "|" in line else ""
                        break
                lines.append(f"- [{d.name}]({d.name}/daily-summary.md) — {commit_info}")
            else:
                lines.append(f"- {d.name} — _report non disponibile_")

        lines.append("")

    output_path = reports_path / "INDEX.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return str(output_path)
