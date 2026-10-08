"""
html_report.py — Dashboard HTML della daily review

Genera `reports/<data>/dashboard.html`: una pagina autonoma (nessun CDN,
nessuna richiesta di rete — i dati sono del cliente e non devono uscire da
qui) che rende lo stesso contenuto di `daily-summary.md` con grafici SVG
inline, filtri sulla lista commit e vista tabellare per ogni grafico.

Insieme all'HTML viene scritto `dashboard-data.json`, il modello dati
serializzato: serve a ri-generare la dashboard dopo che il digest è stato
scritto, senza ri-eseguire l'analisi AI.

    python3 scripts/html_report.py --date 2026-08-26

Palette: istanza di riferimento dataviz. L'unico slot categorico usato è il
n.1 (blu, validato light+dark); le severità usano la status palette riservata
con icona + etichetta, mai il colore da solo. Nessun grafico codifica identità
tramite colore, quindi non serve il canale texture.
"""

import os
import re
import json
import html
import argparse
from datetime import datetime, timedelta
from pathlib import Path


# ── Severità: status palette riservata + icona + etichetta ──
# info non è uno "stato buono": prende il grigio de-emphasis, non un colore
# di serie. Una giornata pulita prende status/good.
SEVERITY_META = {
    "critical": {"label": "Critical", "icon": "⛔", "color": "var(--status-critical)"},
    "error":    {"label": "Error",    "icon": "✖",  "color": "var(--status-serious)"},
    "warning":  {"label": "Warning",  "icon": "▲",  "color": "var(--status-warning)"},
    "info":     {"label": "Info",     "icon": "ℹ",  "color": "var(--text-muted)"},
}
SEVERITY_ORDER = ["critical", "error", "warning", "info"]

QUALITY_BANDS = [
    (10, "Eccellente"), (8, "Ottimo"), (6, "Buono"),
    (4, "Sufficiente"), (1, "Scarso"),
]


def quality_band(score: float) -> str:
    for threshold, label in QUALITY_BANDS:
        if score >= threshold:
            return label
    return "n/d"


# ══════════════════════════════════════════════════════════════
#  MODELLO DATI
# ══════════════════════════════════════════════════════════════

def build_model(repo_reports: list, repo_reviews: list, target_date: str,
                reports_dir: str, trend_days: int = 7,
                kb_section: str = "") -> dict:
    """Costruisce il modello dati della dashboard dagli oggetti dell'engine."""
    reviews_by_name = {r.repo_name: r for r in repo_reviews if r}

    authors: dict = {}
    findings_by_severity = {s: 0 for s in SEVERITY_ORDER}
    gate_counts: dict = {}
    all_scores: list = []
    repos: list = []

    for report in repo_reports:
        review = reviews_by_name.get(report.name)
        reviews_by_hash = {}
        if review:
            for cr in review.commit_reviews:
                reviews_by_hash[cr.commit_hash] = cr

        repo_scores = []
        by_branch: dict = {}

        for c in report.commits:
            cr = reviews_by_hash.get(c.short_hash)
            if cr is None:
                for key, candidate in reviews_by_hash.items():
                    if c.hash.startswith(key):
                        cr = candidate
                        break

            findings = []
            worst = None
            for f in (cr.findings if cr else []):
                sev = f.severity if f.severity in findings_by_severity else "info"
                findings_by_severity[sev] += 1
                gate_counts[f.rule_id] = gate_counts.get(f.rule_id, 0) + 1
                findings.append({
                    "rule_id": f.rule_id, "severity": sev, "file": f.file,
                    "line": f.line, "message": f.message,
                    "suggestion": f.suggestion,
                })
                if worst is None or SEVERITY_ORDER.index(sev) < SEVERITY_ORDER.index(worst):
                    worst = sev

            score = cr.quality_score if cr and cr.quality_score else None
            if score:
                all_scores.append(score)
                repo_scores.append(score)

            entry = authors.setdefault(c.author, {"commits": 0, "scores": []})
            entry["commits"] += 1
            if score:
                entry["scores"].append(score)

            by_branch.setdefault(c.branch, []).append({
                "short_hash": c.short_hash, "message": c.message,
                "author": c.author, "date": c.date[:19],
                "files_changed": c.files_changed,
                "insertions": c.insertions, "deletions": c.deletions,
                "quality_score": score,
                "summary": cr.summary if cr else "",
                "findings": findings,
                "worst_severity": worst or "clean",
            })

        repos.append({
            "name": report.name,
            "summary": review.overall_summary if review else "",
            "positive_notes": (review.stats or {}).get("positive_notes", []) if review else [],
            "critical_issues": review.critical_issues if review else [],
            "errors": report.errors,
            "branches_scanned": len(report.branches_scanned),
            "quality_avg": round(sum(repo_scores) / len(repo_scores), 1) if repo_scores else None,
            "branches": [{"name": b, "commits": cs} for b, cs in sorted(by_branch.items())],
        })

    totals = {
        "commits": sum(len(r.commits) for r in repo_reports),
        "files": sum(c.files_changed for r in repo_reports for c in r.commits),
        "insertions": sum(c.insertions for r in repo_reports for c in r.commits),
        "deletions": sum(c.deletions for r in repo_reports for c in r.commits),
    }

    return {
        "date": target_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "totals": totals,
        "authors": {
            name: {
                "commits": d["commits"],
                "quality_avg": round(sum(d["scores"]) / len(d["scores"]), 1) if d["scores"] else None,
            }
            for name, d in sorted(authors.items())
        },
        "findings_by_severity": findings_by_severity,
        "gate_counts": dict(sorted(gate_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "quality_avg": round(sum(all_scores) / len(all_scores), 1) if all_scores else None,
        "repos": repos,
        "trend": _load_trend_series(reports_dir, target_date, trend_days),
        "kb_section": kb_section,
    }


def _load_trend_series(reports_dir: str, target_date: str, days: int) -> list:
    """
    Serie storica per i grafici di trend: un punto per giorno, il più recente
    per ultimo. `quality` è None per i giorni senza commit — la linea si
    interrompe invece di interpolare un valore che non esiste.
    """
    series = []
    base = datetime.strptime(target_date, "%Y-%m-%d")

    for i in range(days, -1, -1):
        day = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        day_dir = Path(reports_dir) / day
        if not day_dir.is_dir():
            continue

        commits = files = insertions = deletions = 0
        for json_path in sorted(day_dir.glob("*-commits.json")):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    stats = json.load(f).get("stats", {})
            except (json.JSONDecodeError, OSError):
                continue
            commits += stats.get("total_commits", 0)
            files += stats.get("total_files_changed", 0)
            insertions += stats.get("total_insertions", 0)
            deletions += stats.get("total_deletions", 0)

        series.append({
            "date": day, "commits": commits, "files": files,
            "insertions": insertions, "deletions": deletions,
            "quality": _quality_from_summary(day_dir / "daily-summary.md"),
        })

    return series


def _quality_from_summary(summary_path: Path):
    """
    Estrae la quality media da un report già scritto, leggendo solo i punteggi
    per-commit (`— N/10`) e ignorando i N/10 che compaiono nelle tabelle di
    trend degli altri report.
    """
    if not summary_path.exists():
        return None
    try:
        content = summary_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    scores = [int(s) for s in re.findall(r"—\s*(\d+)/10\s", content)]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


# ══════════════════════════════════════════════════════════════
#  GRAFICI SVG
# ══════════════════════════════════════════════════════════════

CHART_W, CHART_H = 680, 250
PAD_L, PAD_R, PAD_T, PAD_B = 46, 22, 18, 46


def _nice_max(value: int) -> int:
    """Arrotonda il massimo dell'asse a un numero leggibile."""
    if value <= 4:
        return max(value, 1)
    for step in (5, 10, 20, 25, 50, 100, 250, 500, 1000):
        if value <= step:
            return step
    return int(((value // 1000) + 1) * 1000)


def _column_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Colonna con testa arrotondata (4px) e base quadra sulla baseline."""
    r = max(0.0, min(r, w / 2, h))
    return (f"M{x:.1f},{y + h:.1f} V{y + r:.1f} "
            f"A{r:.1f},{r:.1f} 0 0 1 {x + r:.1f},{y:.1f} "
            f"H{x + w - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} "
            f"V{y + h:.1f} Z")


def _hbar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Barra orizzontale con punta arrotondata (4px) e base quadra a sinistra."""
    r = max(0.0, min(r, h / 2, w))
    return (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} "
            f"A{r:.1f},{r:.1f} 0 0 1 {x + w:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} A{r:.1f},{r:.1f} 0 0 1 {x + w - r:.1f},{y + h:.1f} "
            f"H{x:.1f} Z")


def _dm(date_str: str) -> str:
    return f"{date_str[8:10]}/{date_str[5:7]}"


def _figure(title: str, subtitle: str, svg: str, table: str) -> str:
    return f"""<figure class="card chart">
  <figcaption>
    <h3>{html.escape(title)}</h3>
    <p class="sub">{html.escape(subtitle)}</p>
  </figcaption>
  {svg}
  <details class="tableview">
    <summary>Vista tabellare</summary>
    {table}
  </details>
</figure>"""


def _empty_chart(title: str, subtitle: str, message: str) -> str:
    return f"""<figure class="card chart">
  <figcaption>
    <h3>{html.escape(title)}</h3>
    <p class="sub">{html.escape(subtitle)}</p>
  </figcaption>
  <p class="empty">{html.escape(message)}</p>
</figure>"""


def _chart_quality_trend(trend: list) -> str:
    """Linea singola: quality media per giorno. Nessuna legenda (una serie)."""
    pts = [t for t in trend]
    if not any(t["quality"] is not None for t in pts):
        return _empty_chart("Quality media per giorno",
                            "Scala 1–10 · i giorni senza commit non hanno punteggio",
                            "Nessun punteggio disponibile nello storico.")

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B
    n = len(pts)
    step = plot_w / max(n - 1, 1)

    def px(i):
        return PAD_L + i * step if n > 1 else PAD_L + plot_w / 2

    def py(v):
        return PAD_T + plot_h - (v / 10.0) * plot_h

    parts = []
    # griglia + tick asse Y (hairline solide, recessive)
    for tick in (0, 2, 4, 6, 8, 10):
        y = py(tick)
        parts.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" '
                     f'x2="{PAD_L + plot_w}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L - 10}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{tick}</text>')

    # segmenti: si interrompono sui giorni senza punteggio
    segment, segments = [], []
    for i, t in enumerate(pts):
        if t["quality"] is None:
            if len(segment) > 1:
                segments.append(segment)
            segment = []
        else:
            segment.append((px(i), py(t["quality"])))
    if len(segment) > 1:
        segments.append(segment)

    for seg in segments:
        d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in seg)
        parts.append(f'<path class="line" d="{d}"/>')

    # etichette asse X
    for i, t in enumerate(pts):
        parts.append(f'<text class="tick" x="{px(i):.1f}" '
                     f'y="{PAD_T + plot_h + 22}" text-anchor="middle">{_dm(t["date"])}</text>')

    # marker (r=5 → 10px) con anello di superficie 2px + hit target 24px
    last_scored = max((i for i, t in enumerate(pts) if t["quality"] is not None), default=None)
    for i, t in enumerate(pts):
        if t["quality"] is None:
            parts.append(f'<text class="tick gap" x="{px(i):.1f}" y="{py(0) - 6:.1f}" '
                         f'text-anchor="middle">–</text>')
            continue
        x, y = px(i), py(t["quality"])
        tip = f'{_dm(t["date"])}\nQuality {t["quality"]}/10\n{t["commits"]} commit'
        parts.append(
            f'<g class="mark" tabindex="0" data-tip="{html.escape(tip)}">'
            f'<circle class="hit" cx="{x:.1f}" cy="{y:.1f}" r="12"/>'
            f'<circle class="dot" cx="{x:.1f}" cy="{y:.1f}" r="5"/></g>')
        if i == last_scored:  # etichetta diretta solo sull'ultimo punto
            parts.append(f'<text class="datalabel" x="{x - 10:.1f}" y="{y - 12:.1f}" '
                         f'text-anchor="end">{t["quality"]}</text>')

    svg = (f'<svg class="chart-svg" viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
           f'aria-label="Quality media per giorno, scala 1-10">'
           + "".join(parts) + "</svg>")

    rows = "".join(
        f'<tr><td>{t["date"]}</td><td class="num">{t["commits"]}</td>'
        f'<td class="num">{t["quality"] if t["quality"] is not None else "n/d"}</td></tr>'
        for t in pts)
    table = ('<table><thead><tr><th>Data</th><th class="num">Commit</th>'
             f'<th class="num">Quality</th></tr></thead><tbody>{rows}</tbody></table>')

    return _figure("Quality media per giorno",
                   "Scala 1–10 · i giorni senza commit non hanno punteggio",
                   svg, table)


def _chart_commits_trend(trend: list) -> str:
    """Colonne: commit per giorno. Una sola serie, un solo colore."""
    if not trend:
        return _empty_chart("Commit per giorno", "Volume giornaliero",
                            "Nessuno storico disponibile.")

    plot_w = CHART_W - PAD_L - PAD_R
    plot_h = CHART_H - PAD_T - PAD_B
    top = _nice_max(max(t["commits"] for t in trend))
    n = len(trend)
    band = plot_w / n
    bar_w = min(24.0, band * 0.55)  # mai tutta la banda: il resto è aria

    parts = []
    for tick in (0, top // 2, top) if top > 1 else (0, top):
        y = PAD_T + plot_h - (tick / top) * plot_h
        parts.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" '
                     f'x2="{PAD_L + plot_w}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L - 10}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{tick}</text>')

    for i, t in enumerate(trend):
        cx = PAD_L + band * i + band / 2
        parts.append(f'<text class="tick" x="{cx:.1f}" y="{PAD_T + plot_h + 22}" '
                     f'text-anchor="middle">{_dm(t["date"])}</text>')
        tip = (f'{_dm(t["date"])}\n{t["commits"]} commit\n'
               f'+{t["insertions"]} / -{t["deletions"]} righe')
        if t["commits"] == 0:
            parts.append(f'<g class="mark" tabindex="0" data-tip="{html.escape(tip)}">'
                         f'<rect class="hit" x="{cx - 12:.1f}" y="{PAD_T}" width="24" '
                         f'height="{plot_h}"/></g>')
            parts.append(f'<text class="tick gap" x="{cx:.1f}" '
                         f'y="{PAD_T + plot_h - 6}" text-anchor="middle">0</text>')
            continue
        h = (t["commits"] / top) * plot_h
        y = PAD_T + plot_h - h
        parts.append(
            f'<g class="mark" tabindex="0" data-tip="{html.escape(tip)}">'
            f'<rect class="hit" x="{cx - 12:.1f}" y="{PAD_T}" width="24" height="{plot_h}"/>'
            f'<path class="bar" d="{_column_path(cx - bar_w / 2, y, bar_w, h)}"/></g>')
        parts.append(f'<text class="datalabel" x="{cx:.1f}" y="{y - 8:.1f}" '
                     f'text-anchor="middle">{t["commits"]}</text>')

    parts.append(f'<line class="axis" x1="{PAD_L}" y1="{PAD_T + plot_h}" '
                 f'x2="{PAD_L + plot_w}" y2="{PAD_T + plot_h}"/>')

    svg = (f'<svg class="chart-svg" viewBox="0 0 {CHART_W} {CHART_H}" role="img" '
           f'aria-label="Commit per giorno">' + "".join(parts) + "</svg>")

    rows = "".join(
        f'<tr><td>{t["date"]}</td><td class="num">{t["commits"]}</td>'
        f'<td class="num">{t["files"]}</td><td class="num">+{t["insertions"]}</td>'
        f'<td class="num">-{t["deletions"]}</td></tr>' for t in trend)
    table = ('<table><thead><tr><th>Data</th><th class="num">Commit</th>'
             '<th class="num">File</th><th class="num">Add</th><th class="num">Del</th>'
             f'</tr></thead><tbody>{rows}</tbody></table>')

    return _figure("Commit per giorno", "Volume giornaliero sui repo monitorati",
                   svg, table)


def _chart_hbars(title: str, subtitle: str, items: list, unit: str,
                 axis_max: float = None, empty_msg: str = "Nessun dato.") -> str:
    """
    Barre orizzontali, una serie sola → un solo colore per tutte le barre
    (mai una rampa di valore su categorie nominali).
    `items`: [(etichetta, valore, tooltip)]
    """
    if not items:
        return _empty_chart(title, subtitle, empty_msg)

    row_h, gap = 30, 10
    label_w = 190
    height = PAD_T + len(items) * (row_h + gap) + 30
    plot_w = CHART_W - label_w - PAD_R - 44
    top = axis_max or _nice_max(int(max(v for _, v, _ in items)))

    parts = []
    for i, (label, value, tip) in enumerate(items):
        y = PAD_T + i * (row_h + gap)
        shown = label if len(label) <= 26 else label[:25] + "…"
        parts.append(f'<text class="barlabel" x="{label_w - 12}" y="{y + row_h / 2 + 4:.1f}" '
                     f'text-anchor="end">{html.escape(shown)}</text>')
        w = (value / top) * plot_w if top else 0
        parts.append(
            f'<g class="mark" tabindex="0" data-tip="{html.escape(tip)}">'
            f'<rect class="hit" x="{label_w}" y="{y}" width="{plot_w + 44}" height="{row_h}"/>'
            + (f'<path class="bar" d="{_hbar_path(label_w, y + 3, w, row_h - 6)}"/>'
               if w > 0.5 else "")
            + '</g>')
        parts.append(f'<text class="datalabel" x="{label_w + w + 10:.1f}" '
                     f'y="{y + row_h / 2 + 4:.1f}">{value}{unit}</text>')

    svg = (f'<svg class="chart-svg" viewBox="0 0 {CHART_W} {height}" role="img" '
           f'aria-label="{html.escape(title)}">' + "".join(parts) + "</svg>")

    rows = "".join(f'<tr><td>{html.escape(l)}</td><td class="num">{v}{unit}</td></tr>'
                   for l, v, _ in items)
    table = ('<table><thead><tr><th>Voce</th><th class="num">Valore</th></tr></thead>'
             f'<tbody>{rows}</tbody></table>')

    return _figure(title, subtitle, svg, table)


# ══════════════════════════════════════════════════════════════
#  MARKDOWN → HTML (minimale, zero dipendenze)
# ══════════════════════════════════════════════════════════════

def md_to_html(text: str, heading_offset: int = 2) -> str:
    """
    Converte il sottoinsieme di Markdown usato nei digest e nella sezione KB.
    `heading_offset` scala i livelli dei titoli per non spezzare la gerarchia
    della pagina che li ospita.
    """
    if not text or not text.strip():
        return ""

    out, lines = [], text.strip().split("\n")
    i = 0
    list_open = quote_open = False

    def close_blocks():
        nonlocal list_open, quote_open
        if list_open:
            out.append("</ul>")
            list_open = False
        if quote_open:
            out.append("</blockquote>")
            quote_open = False

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        if not stripped:
            close_blocks()
            i += 1
            continue

        # tabella
        if stripped.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            close_blocks()
            header = [c.strip() for c in stripped.strip("|").split("|")]
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            rows_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                for r in body)
            out.append(f'<div class="md-table"><table><thead><tr>{head_html}</tr>'
                       f"</thead><tbody>{rows_html}</tbody></table></div>")
            continue

        if re.match(r"^-{3,}$", stripped):
            close_blocks()
            out.append("<hr>")
            i += 1
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            close_blocks()
            level = min(len(heading.group(1)) + heading_offset, 6)
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue

        if stripped.startswith(">"):
            if list_open:
                out.append("</ul>")
                list_open = False
            if not quote_open:
                out.append("<blockquote>")
                quote_open = True
            out.append(f"<p>{_inline(stripped.lstrip('>').strip())}</p>")
            i += 1
            continue

        item = re.match(r"^[-*]\s+(.*)$", stripped)
        if item:
            if quote_open:
                out.append("</blockquote>")
                quote_open = False
            if not list_open:
                out.append("<ul>")
                list_open = True
            out.append(f"<li>{_inline(item.group(1))}</li>")
            i += 1
            continue

        close_blocks()
        out.append(f"<p>{_inline(stripped)}</p>")
        i += 1

    close_blocks()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Formattazione inline: i code span sono protetti prima del resto."""
    spans = []

    def stash(match):
        spans.append(match.group(1))
        return f"\x00{len(spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<em>\1</em>", text)
    text = re.sub(r"\x00(\d+)\x00",
                  lambda m: f"<code>{html.escape(spans[int(m.group(1))])}</code>", text)
    return text


# ══════════════════════════════════════════════════════════════
#  RENDER
# ══════════════════════════════════════════════════════════════

def render_html(model: dict, digest_md: str = "") -> str:
    date = model["date"]
    totals = model["totals"]
    sev = model["findings_by_severity"]
    total_findings = sum(sev.values())
    quality = model["quality_avg"]
    trend = model["trend"]

    # ── hero: quality del giorno + delta sull'ultimo giorno con punteggio ──
    prev = next((t["quality"] for t in reversed(trend[:-1])
                 if t["quality"] is not None), None) if len(trend) > 1 else None
    if quality is None:
        hero_value, hero_unit = "n/d", ""
        hero_note = "Nessun commit da valutare oggi"
    else:
        hero_value, hero_unit = f"{quality}", "/10"
        if prev is None:
            hero_note = f"{quality_band(quality)} · nessun riferimento precedente"
        else:
            delta = round(quality - prev, 1)
            arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
            word = "in miglioramento" if delta > 0 else ("in calo" if delta < 0 else "stabile")
            cls = "up" if delta > 0 else ("down" if delta < 0 else "flat")
            hero_note = (f'{quality_band(quality)} · <span class="delta {cls}">{arrow} '
                         f'{abs(delta):+.1f}</span> vs {prev}/10 — {word}')

    # ── KPI ──
    kpis = [
        ("Commit", totals["commits"], f'{len(model["repos"])} repo monitorati'),
        ("Autori attivi", len(model["authors"]),
         ", ".join(model["authors"].keys()) or "nessuno"),
        ("File modificati", totals["files"], "nel giorno"),
        ("Righe aggiunte", f'+{totals["insertions"]}', "inserimenti"),
        ("Righe rimosse", f'-{totals["deletions"]}', "cancellazioni"),
        ("Segnalazioni", total_findings,
         "nessuna violazione" if total_findings == 0 else "sui quality gate"),
    ]
    kpi_html = "".join(
        f'<div class="tile"><p class="tile-label">{html.escape(label)}</p>'
        f'<p class="tile-value">{html.escape(str(value))}</p>'
        f'<p class="tile-sub">{html.escape(sub)}</p></div>'
        for label, value, sub in kpis)

    # ── striscia severità: pallino + icona + etichetta + conteggio ──
    if total_findings == 0:
        status_html = ('<div class="chip clean"><span class="ico">✓</span>'
                       '<span class="chip-label">Nessuna segnalazione</span>'
                       '<span class="chip-count">0</span></div>')
    else:
        status_html = "".join(
            f'<div class="chip" style="--chip:{SEVERITY_META[s]["color"]}">'
            f'<span class="ico">{SEVERITY_META[s]["icon"]}</span>'
            f'<span class="chip-label">{SEVERITY_META[s]["label"]}</span>'
            f'<span class="chip-count">{sev[s]}</span></div>'
            for s in SEVERITY_ORDER if sev[s] > 0)

    # ── grafici ──
    gate_items = [(rid, cnt, f"{rid}\n{cnt} violazioni oggi")
                  for rid, cnt in list(model["gate_counts"].items())[:8]]
    author_items = [
        (name, d["quality_avg"], f'{name}\nQuality media {d["quality_avg"]}/10\n{d["commits"]} commit')
        for name, d in model["authors"].items() if d["quality_avg"] is not None
    ]

    charts = [
        _chart_quality_trend(trend),
        _chart_commits_trend(trend),
        _chart_hbars("Quality gate violati", "Gate con più segnalazioni oggi",
                     gate_items, "", empty_msg="Nessun gate violato oggi."),
        _chart_hbars("Quality media per autore", "Media dei punteggi dei commit di oggi",
                     author_items, "/10", axis_max=10,
                     empty_msg="Nessun punteggio per autore oggi."),
    ]

    # ── filtri (una sola riga, sopra la lista che filtra) ──
    repo_opts = "".join(f'<option value="{html.escape(r["name"])}">{html.escape(r["name"])}</option>'
                        for r in model["repos"])
    author_opts = "".join(f'<option value="{html.escape(a)}">{html.escape(a)}</option>'
                          for a in model["authors"])
    sev_opts = "".join(f'<option value="{s}">{SEVERITY_META[s]["label"]}</option>'
                       for s in SEVERITY_ORDER if sev[s] > 0)

    # ── sezioni per repo ──
    repo_sections = []
    for repo in model["repos"]:
        head = [f'<div class="repo-head"><h3>{html.escape(repo["name"])}</h3>']
        if repo["quality_avg"] is not None:
            head.append(f'<span class="badge">{repo["quality_avg"]}/10</span>')
        head.append(f'<span class="muted">{repo["branches_scanned"]} branch '
                    f'scansionati</span></div>')
        blocks = ["".join(head)]

        if repo["errors"]:
            items = "".join(f"<li>{html.escape(e)}</li>" for e in repo["errors"])
            blocks.append(f'<div class="notice warn"><strong>Errori durante la raccolta</strong>'
                          f"<ul>{items}</ul></div>")

        if repo["critical_issues"]:
            items = "".join(f"<li>{_inline(c)}</li>" for c in repo["critical_issues"])
            blocks.append(f'<div class="notice crit"><strong>Issue critici</strong>'
                          f"<ul>{items}</ul></div>")

        if repo["summary"]:
            blocks.append(f'<p class="ai-summary">{_inline(repo["summary"])}</p>')

        if repo["positive_notes"]:
            items = "".join(f"<li>{_inline(n)}</li>" for n in repo["positive_notes"])
            blocks.append(f'<div class="notice good"><strong>Cose fatte bene</strong>'
                          f"<ul>{items}</ul></div>")

        if not repo["branches"]:
            blocks.append('<p class="empty">Nessun commit per questa data.</p>')

        for branch in repo["branches"]:
            cards = "".join(_commit_card(c, repo["name"]) for c in branch["commits"])
            blocks.append(
                f'<div class="branch"><h4><span class="leaf">🌿</span>'
                f'<code>{html.escape(branch["name"])}</code>'
                f'<span class="muted">{len(branch["commits"])} commit</span></h4>'
                f'{cards}</div>')

        repo_sections.append(f'<section class="card repo" data-repo="{html.escape(repo["name"])}">'
                             + "".join(blocks) + "</section>")

    # ── tabella completa dei commit (vista WCAG-clean della lista) ──
    commit_rows = "".join(
        f'<tr><td><code>{html.escape(c["short_hash"])}</code></td>'
        f'<td>{html.escape(c["message"])}</td><td>{html.escape(c["author"])}</td>'
        f'<td>{html.escape(repo["name"])}</td>'
        f'<td class="num">{c["quality_score"] if c["quality_score"] else "n/d"}</td>'
        f'<td class="num">{len(c["findings"])}</td></tr>'
        for repo in model["repos"] for b in repo["branches"] for c in b["commits"])
    commit_table = (
        '<details class="tableview wide"><summary>Vista tabellare di tutti i commit</summary>'
        '<table><thead><tr><th>Hash</th><th>Messaggio</th><th>Autore</th><th>Repo</th>'
        '<th class="num">Quality</th><th class="num">Finding</th></tr></thead>'
        f"<tbody>{commit_rows}</tbody></table></details>"
        if commit_rows else "")

    digest_html = md_to_html(digest_md)
    digest_block = (f'<section class="card prose"><h2>Digest del giorno</h2>{digest_html}</section>'
                    if digest_html else
                    '<section class="card prose"><h2>Digest del giorno</h2>'
                    '<p class="empty">Digest non ancora scritto. Dopo averlo creato in '
                    '<code>digest.md</code>, rigenera con '
                    f'<code>python3 scripts/html_report.py --date {date}</code>.</p></section>')

    # offset 1: la sezione KB porta già il proprio "## Knowledge Base Updates",
    # che deve diventare un h3 sotto l'h2 della pagina.
    kb_html = md_to_html(model.get("kb_section", ""), heading_offset=1)
    kb_block = (f'<section class="card prose">{kb_html}</section>' if kb_html else "")
    notice_html = md_to_html(model.get("notice", ""), heading_offset=0)
    notice_block = (f'<section class="card prose">{notice_html}</section>' if notice_html else "")

    return _PAGE.format(
        date=date,
        generated_at=model["generated_at"],
        hero_value=hero_value,
        hero_unit=hero_unit,
        hero_note=hero_note,
        kpis=kpi_html,
        status=status_html,
        charts="".join(charts),
        repo_opts=repo_opts,
        author_opts=author_opts,
        sev_opts=sev_opts,
        sev_filter_disabled="" if sev_opts else " disabled",
        repos="".join(repo_sections),
        commit_table=commit_table,
        digest=notice_block + digest_block,
        kb=kb_block,
        css=_CSS,
        js=_JS,
    )


def _commit_card(c: dict, repo_name: str) -> str:
    sev_list = sorted({f["severity"] for f in c["findings"]},
                      key=SEVERITY_ORDER.index)
    score = c["quality_score"]
    badge = (f'<span class="badge">{score}/10 · {quality_band(score)}</span>'
             if score else '<span class="badge muted-badge">n/d</span>')

    findings_html = ""
    if c["findings"]:
        rows = []
        for f in c["findings"]:
            meta = SEVERITY_META[f["severity"]]
            detail = []
            if f["file"]:
                loc = f'{f["file"]}:{f["line"]}' if f.get("line") else f["file"]
                detail.append(f'<p class="fpath"><code>{html.escape(str(loc))}</code></p>')
            if f["suggestion"]:
                detail.append(f'<p class="fsug">{_inline(f["suggestion"])}</p>')
            rows.append(
                f'<li class="finding" style="--chip:{meta["color"]}">'
                f'<p class="fhead"><span class="ico">{meta["icon"]}</span>'
                f'<span class="fsev">{meta["label"]}</span>'
                f'<code class="gate">{html.escape(f["rule_id"])}</code></p>'
                f'<p class="fmsg">{_inline(f["message"])}</p>'
                + "".join(detail) + "</li>")
        findings_html = f'<ul class="findings">{"".join(rows)}</ul>'

    return f"""<article class="commit" data-repo="{html.escape(repo_name)}"
   data-author="{html.escape(c["author"])}"
   data-sev="{" ".join(sev_list)}"
   data-text="{html.escape((c["message"] + " " + c["short_hash"]).lower())}">
  <header>
    <code class="hash">{html.escape(c["short_hash"])}</code>
    <span class="cmsg">{html.escape(c["message"])}</span>
    {badge}
  </header>
  <p class="cmeta">{html.escape(c["author"])} · {html.escape(c["date"])} ·
     {c["files_changed"]} file · <span class="ins">+{c["insertions"]}</span>
     <span class="del">-{c["deletions"]}</span></p>
  {f'<p class="csum">{_inline(c["summary"])}</p>' if c["summary"] else ""}
  {findings_html}
</article>"""


# ══════════════════════════════════════════════════════════════
#  CSS / JS / TEMPLATE
# ══════════════════════════════════════════════════════════════

_CSS = """
:root {
  color-scheme: light;
  --plane: #f9f9f7;
  --surface-1: #fcfcfb;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --status-good: #0ca30c;
  --status-warning: #fab219;
  --status-serious: #ec835a;
  --status-critical: #d03b3b;
  --delta-up: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --plane: #0d0d0d;
    --surface-1: #1a1a19;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --delta-up: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --plane: #0d0d0d;
  --surface-1: #1a1a19;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --delta-up: #0ca30c;
}

* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 20px 64px;
  background: var(--plane); color: var(--text-primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 1180px; margin: 0 auto; }
a { color: var(--series-1); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.88em; }
h1, h2, h3, h4 { line-height: 1.25; }
.muted { color: var(--text-muted); font-weight: 400; font-size: 13px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.empty { color: var(--text-muted); font-style: italic; margin: 8px 0 0; }

/* ── header ── */
.topbar {
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
  padding: 28px 0 20px;
}
.topbar h1 { margin: 0; font-size: 21px; letter-spacing: -0.01em; }
.topbar .when { color: var(--text-muted); font-size: 13px; }
.topbar .spacer { flex: 1; }
button.ghost {
  background: var(--surface-1); color: var(--text-secondary);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 13px; font: inherit; font-size: 13px; cursor: pointer;
}
button.ghost:hover { color: var(--text-primary); }

/* ── card ── */
.card {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 14px; padding: 20px 22px; margin-bottom: 18px;
}

/* ── hero ── */
.hero { display: flex; align-items: center; gap: 28px; flex-wrap: wrap; }
.hero-fig { display: flex; align-items: baseline; gap: 6px; }
.hero-fig .v { font-size: 58px; font-weight: 600; letter-spacing: -0.03em; }
.hero-fig .u { font-size: 20px; color: var(--text-muted); }
.hero-meta .lab {
  margin: 0 0 4px; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--text-muted);
}
.hero-meta .note { margin: 0; color: var(--text-secondary); font-size: 14px; }
.delta { font-weight: 600; }
.delta.up { color: var(--delta-up); }
.delta.down { color: var(--status-critical); }
.delta.flat { color: var(--text-muted); }

/* ── KPI + chip ── */
.kpis {
  display: grid; gap: 12px; margin-bottom: 18px;
  grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
}
.tile {
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px;
}
.tile-label {
  margin: 0 0 6px; font-size: 12px; color: var(--text-muted);
  text-transform: uppercase; letter-spacing: 0.06em;
}
.tile-value { margin: 0; font-size: 27px; font-weight: 600; letter-spacing: -0.02em; }
.tile-sub {
  margin: 4px 0 0; font-size: 12px; color: var(--text-secondary);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chips { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
.chip {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--surface-1); border: 1px solid var(--border);
  border-radius: 999px; padding: 7px 14px 7px 11px; font-size: 13px;
}
.chip::before {
  content: ""; width: 9px; height: 9px; border-radius: 50%;
  background: var(--chip, var(--text-muted));
}
.chip.clean { --chip: var(--status-good); }
.chip .ico { font-size: 12px; color: var(--text-secondary); }
.chip-label { color: var(--text-secondary); }
.chip-count { font-weight: 600; font-variant-numeric: tabular-nums; }

/* ── grafici ── */
.charts { display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); }
.chart figcaption { margin-bottom: 10px; }
.chart figcaption h3 { margin: 0; font-size: 15px; }
.chart figcaption .sub { margin: 3px 0 0; font-size: 12.5px; color: var(--text-muted); }
.chart-svg { width: 100%; height: auto; display: block; overflow: visible; }
.grid { stroke: var(--grid); stroke-width: 1; }
.axis { stroke: var(--axis); stroke-width: 1; }
.tick { fill: var(--text-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tick.gap { fill: var(--text-muted); font-size: 10px; }
.barlabel { fill: var(--text-secondary); font-size: 12px; }
.datalabel {
  fill: var(--text-primary); font-size: 12px; font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.line { fill: none; stroke: var(--series-1); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.dot { fill: var(--series-1); stroke: var(--surface-1); stroke-width: 2; }
.bar { fill: var(--series-1); }
.hit { fill: transparent; }
.mark { cursor: default; outline: none; }
.mark:hover .bar, .mark:focus-visible .bar { fill-opacity: 0.82; }
.mark:focus-visible .dot, .mark:focus-visible .bar { stroke: var(--text-primary); stroke-width: 2; }

/* ── tabelle ── */
.tableview { margin-top: 12px; }
.tableview summary {
  cursor: pointer; font-size: 12.5px; color: var(--text-muted);
  padding: 3px 0; list-style: none;
}
.tableview summary::before { content: "▸ "; }
.tableview[open] summary::before { content: "▾ "; }
.tableview summary:hover { color: var(--text-primary); }
table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--text-muted); font-weight: 500; font-size: 12px; }
td { color: var(--text-secondary); }
.md-table { overflow-x: auto; }

/* ── filtri ── */
.filters {
  display: flex; gap: 12px; align-items: flex-end; flex-wrap: wrap;
  padding: 16px 18px; margin-bottom: 18px;
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 14px;
}
.filters .f { display: flex; flex-direction: column; gap: 5px; }
.filters label {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--text-muted);
}
.filters select, .filters input {
  background: var(--plane); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 7px 9px; font: inherit; font-size: 13px; min-width: 150px;
}
.filters .count { margin-left: auto; font-size: 13px; color: var(--text-muted); }

/* ── repo / commit ── */
.repo-head {
  display: flex; align-items: center; gap: 12px;
  flex-wrap: wrap; margin-bottom: 12px;
}
.repo-head h3 { margin: 0; font-size: 17px; }
.badge {
  background: color-mix(in srgb, var(--series-1) 14%, transparent);
  color: var(--text-primary); border: 1px solid var(--border);
  border-radius: 999px; padding: 3px 10px; font-size: 12px; font-weight: 600;
  white-space: nowrap;
}
.badge.muted-badge { color: var(--text-muted); background: transparent; font-weight: 400; }
.ai-summary { color: var(--text-secondary); margin: 10px 0 14px; }
.notice {
  border-left: 3px solid var(--text-muted); border-radius: 0 8px 8px 0;
  padding: 10px 14px; margin: 12px 0; font-size: 13.5px;
  background: color-mix(in srgb, var(--text-muted) 7%, transparent);
}
.notice strong { display: block; margin-bottom: 4px; font-size: 12.5px; }
.notice ul { margin: 4px 0 0; padding-left: 18px; color: var(--text-secondary); }
.notice.good { border-left-color: var(--status-good); background: color-mix(in srgb, var(--status-good) 7%, transparent); }
.notice.warn { border-left-color: var(--status-warning); background: color-mix(in srgb, var(--status-warning) 9%, transparent); }
.notice.crit { border-left-color: var(--status-critical); background: color-mix(in srgb, var(--status-critical) 8%, transparent); }

.branch { margin: 18px 0 0; }
.branch h4 {
  display: flex; align-items: center; gap: 9px; flex-wrap: wrap;
  margin: 0 0 10px; font-size: 13.5px; font-weight: 500; color: var(--text-secondary);
}
.branch h4 code { color: var(--text-primary); }

.commit {
  border: 1px solid var(--border); border-radius: 11px;
  padding: 13px 15px; margin-bottom: 10px;
}
.commit header { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.commit .hash { color: var(--series-1); font-weight: 600; }
.commit .cmsg { font-weight: 500; flex: 1; min-width: 200px; }
.cmeta { margin: 7px 0 0; font-size: 12.5px; color: var(--text-muted); }
.cmeta .ins { color: var(--delta-up); font-variant-numeric: tabular-nums; }
.cmeta .del { color: var(--status-critical); font-variant-numeric: tabular-nums; }
.csum { margin: 8px 0 0; font-size: 13.5px; color: var(--text-secondary); }

.findings { list-style: none; margin: 12px 0 0; padding: 0; }
.finding {
  border-left: 3px solid var(--chip, var(--text-muted));
  background: color-mix(in srgb, var(--chip, var(--text-muted)) 7%, transparent);
  border-radius: 0 8px 8px 0; padding: 9px 13px; margin-bottom: 8px;
}
.fhead { display: flex; align-items: center; gap: 8px; margin: 0 0 5px; }
.fhead .ico { font-size: 12px; }
.fsev {
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--text-secondary); font-weight: 600;
}
.gate {
  background: color-mix(in srgb, var(--text-muted) 16%, transparent);
  border-radius: 5px; padding: 1px 6px; color: var(--text-primary);
}
.fmsg { margin: 0; font-size: 13.5px; }
.fpath { margin: 5px 0 0; font-size: 12px; color: var(--text-muted); word-break: break-all; }
.fsug { margin: 5px 0 0; font-size: 13px; color: var(--text-secondary); }

/* ── prose (digest / KB) ── */
.prose h2 { margin: 0 0 12px; font-size: 17px; }
.prose h3 { margin: 20px 0 8px; font-size: 14.5px; }
.prose h4 { margin: 16px 0 6px; font-size: 13.5px; color: var(--text-secondary); }
.prose p { margin: 9px 0; color: var(--text-secondary); }
.prose ul { padding-left: 20px; color: var(--text-secondary); }
.prose li { margin: 4px 0; }
.prose blockquote {
  margin: 12px 0; padding: 10px 14px;
  border-left: 3px solid var(--status-warning);
  background: color-mix(in srgb, var(--status-warning) 9%, transparent);
  border-radius: 0 8px 8px 0;
}
.prose blockquote p { margin: 0; color: var(--text-primary); }
.prose hr { border: 0; border-top: 1px solid var(--border); margin: 18px 0; }
.prose strong { color: var(--text-primary); }

/* ── tooltip ── */
#tip {
  position: fixed; z-index: 40; pointer-events: none; opacity: 0;
  transition: opacity 90ms linear; max-width: 260px;
  background: var(--surface-1); color: var(--text-primary);
  border: 1px solid var(--border); border-radius: 9px;
  padding: 8px 11px; font-size: 12.5px; white-space: pre-line;
  box-shadow: 0 6px 20px rgba(0,0,0,0.16);
}
#tip[data-show="1"] { opacity: 1; }

footer.foot {
  margin-top: 26px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--text-muted); font-size: 12.5px;
}

/* ── stampa / forced-colors: il colore non porta mai da solo il significato ── */
@media print {
  body { background: #fff; padding: 0; }
  .card, .tile { break-inside: avoid; border-color: #ccc; }
  .filters, button.ghost, #tip { display: none; }
  .tableview { display: block; }
  .tableview > table, .tableview > * { display: revert; }
}
@media (forced-colors: active) {
  .bar, .dot { forced-color-adjust: none; }
  .card, .tile, .commit, .chip { border: 1px solid CanvasText; }
}
"""

_JS = """
(function () {
  // ── tema: OS di default, override persistito ──
  var root = document.documentElement;
  try {
    var saved = localStorage.getItem('gdr-theme');
    if (saved) root.setAttribute('data-theme', saved);
  } catch (e) {}
  var btn = document.getElementById('theme');
  if (btn) btn.addEventListener('click', function () {
    var isDark = root.getAttribute('data-theme') === 'dark' ||
      (!root.hasAttribute('data-theme') &&
        window.matchMedia('(prefers-color-scheme: dark)').matches);
    var next = isDark ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('gdr-theme', next); } catch (e) {}
  });

  // ── tooltip: solo arricchimento, ogni valore è già nel DOM ──
  var tip = document.getElementById('tip');
  function show(el, x, y) {
    tip.textContent = el.getAttribute('data-tip') || '';
    tip.setAttribute('data-show', '1');
    var r = tip.getBoundingClientRect();
    var left = Math.min(Math.max(8, x + 14), window.innerWidth - r.width - 8);
    var top = y - r.height - 14;
    if (top < 8) top = y + 18;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }
  function hide() { tip.removeAttribute('data-show'); }
  document.addEventListener('mousemove', function (ev) {
    var mark = ev.target.closest ? ev.target.closest('.mark') : null;
    if (mark) show(mark, ev.clientX, ev.clientY); else hide();
  });
  document.addEventListener('focusin', function (ev) {
    var mark = ev.target.closest ? ev.target.closest('.mark') : null;
    if (!mark) { hide(); return; }
    var b = mark.getBoundingClientRect();
    show(mark, b.left + b.width / 2, b.top + 8);
  });
  document.addEventListener('focusout', hide);
  document.addEventListener('scroll', hide, true);

  // ── filtri: scopano la lista commit (i grafici restano sul giorno intero) ──
  var repoSel = document.getElementById('f-repo');
  var authorSel = document.getElementById('f-author');
  var sevSel = document.getElementById('f-sev');
  var q = document.getElementById('f-q');
  var counter = document.getElementById('f-count');
  var commits = Array.prototype.slice.call(document.querySelectorAll('.commit'));
  if (!commits.length || !counter) return;

  function apply() {
    var repo = repoSel.value, author = authorSel.value, sev = sevSel.value;
    var text = (q.value || '').trim().toLowerCase();
    var shown = 0;
    commits.forEach(function (c) {
      var ok = (!repo || c.dataset.repo === repo) &&
        (!author || c.dataset.author === author) &&
        (!sev || (c.dataset.sev || '').split(' ').indexOf(sev) !== -1) &&
        (!text || (c.dataset.text || '').indexOf(text) !== -1);
      c.hidden = !ok;
      if (ok) shown++;
    });
    // nascondi i contenitori rimasti vuoti, così non restano intestazioni orfane
    document.querySelectorAll('.branch').forEach(function (b) {
      b.hidden = !b.querySelector('.commit:not([hidden])');
    });
    document.querySelectorAll('.repo').forEach(function (r) {
      var hasCommits = r.querySelector('.commit');
      r.hidden = hasCommits ? !r.querySelector('.commit:not([hidden])') : false;
    });
    counter.textContent = shown + ' di ' + commits.length + ' commit';
  }

  [repoSel, authorSel, sevSel].forEach(function (s) {
    s.addEventListener('change', apply);
  });
  q.addEventListener('input', apply);
  apply();
})();
"""

_PAGE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Daily Code Review — {date}</title>
<style>{css}</style>
</head>
<body>
<div class="wrap">

  <div class="topbar">
    <h1>Daily Code Review</h1>
    <span class="when">{date} · generato il {generated_at}</span>
    <span class="spacer"></span>
    <button class="ghost" id="theme" type="button">Tema chiaro / scuro</button>
  </div>

  <section class="card hero">
    <div class="hero-fig"><span class="v">{hero_value}</span><span class="u">{hero_unit}</span></div>
    <div class="hero-meta">
      <p class="lab">Quality media del giorno</p>
      <p class="note">{hero_note}</p>
    </div>
  </section>

  <div class="kpis">{kpis}</div>
  <div class="chips">{status}</div>

  <div class="charts">{charts}</div>

  {digest}

  <h2 style="margin:28px 0 14px;font-size:17px;">Commit del giorno</h2>

  <div class="filters">
    <div class="f"><label for="f-repo">Repository</label>
      <select id="f-repo"><option value="">Tutti</option>{repo_opts}</select></div>
    <div class="f"><label for="f-author">Autore</label>
      <select id="f-author"><option value="">Tutti</option>{author_opts}</select></div>
    <div class="f"><label for="f-sev">Severità</label>
      <select id="f-sev"{sev_filter_disabled}><option value="">Tutte</option>{sev_opts}</select></div>
    <div class="f"><label for="f-q">Cerca</label>
      <input id="f-q" type="search" placeholder="messaggio o hash"></div>
    <span class="count" id="f-count"></span>
  </div>

  {repos}
  {commit_table}

  {kb}

  <footer class="foot">
    Report completo in <code>daily-summary.md</code> · pagina autonoma, nessuna
    richiesta di rete: i dati restano in locale.
  </footer>

</div>
<div id="tip" role="tooltip"></div>
<script>{js}</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════
#  API
# ══════════════════════════════════════════════════════════════

def generate_html_report(repo_reports: list, repo_reviews: list, target_date: str,
                         reports_dir: str, trend_days: int = 7,
                         kb_section: str = "", notice: str = "") -> str:
    """
    Genera dashboard.html (+ dashboard-data.json) per la data indicata.
    Chiamata dall'engine dopo la scrittura del report Markdown.
    """
    model = build_model(repo_reports, repo_reviews, target_date,
                        reports_dir, trend_days, kb_section)
    model["notice"] = notice

    output_dir = Path(reports_dir) / target_date
    output_dir.mkdir(parents=True, exist_ok=True)

    (output_dir / "dashboard-data.json").write_text(
        json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")

    digest_path = output_dir / "digest.md"
    digest_md = digest_path.read_text(encoding="utf-8") if digest_path.exists() else ""

    output_path = output_dir / "dashboard.html"
    output_path.write_text(render_html(model, digest_md), encoding="utf-8")
    return str(output_path)


def regenerate(reports_dir: str, target_date: str) -> str:
    """
    Ri-genera dashboard.html da dashboard-data.json, includendo digest.md se
    esiste. Serve dopo la scrittura del digest, senza ri-eseguire l'AI.
    """
    output_dir = Path(reports_dir) / target_date
    data_path = output_dir / "dashboard-data.json"
    if not data_path.exists():
        raise FileNotFoundError(
            f"{data_path} non trovato — esegui prima "
            f"`python3 scripts/daily_review.py --date {target_date}`")

    model = json.loads(data_path.read_text(encoding="utf-8"))
    digest_path = output_dir / "digest.md"
    digest_md = digest_path.read_text(encoding="utf-8") if digest_path.exists() else ""

    output_path = output_dir / "dashboard.html"
    output_path.write_text(render_html(model, digest_md), encoding="utf-8")
    return str(output_path)


def main():
    parser = argparse.ArgumentParser(
        description="Ri-genera la dashboard HTML di una daily review")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="data del report (YYYY-MM-DD, default: oggi)")
    parser.add_argument("--reports-dir", default=None,
                        help="directory dei report (default: <root>/reports)")
    args = parser.parse_args()

    reports_dir = args.reports_dir or str(
        Path(__file__).resolve().parent.parent / "reports")

    try:
        path = regenerate(reports_dir, args.date)
    except FileNotFoundError as e:
        print(f"✗ {e}")
        raise SystemExit(1)

    print(f"✓ Dashboard aggiornata: {path}")

    # Il digest appena scritto entra anche nella base dati del portale.
    try:
        import review_db
        review_db.ingest_day(args.date, Path(reports_dir))
        print("✓ Base dati aggiornata")
    except Exception as e:
        print(f"⚠ Base dati non aggiornata: {e}")


if __name__ == "__main__":
    main()
