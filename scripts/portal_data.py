"""
portal_data.py — Strato dati del portale web delle review

Aggrega tutto ciò che sta sotto `reports/` in un unico dataset a livello di
commit, così che il portale possa filtrare e raggruppare lato client:

- `*-commits.json`      → commit grezzi (autore, branch, file, churn)
- `dashboard-data.json` → esito della review (score, finding, summary)
- `daily-summary.md`    → fallback per i giorni precedenti al modello JSON

Legge anche la topologia dai cloni locali (`git log --all`, mai `fetch`) per
disegnare il git-flow e la timeline dei rilasci (tag).

Tutto in sola lettura: nessuna scrittura su report, KB o repository.
"""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT_DIR / "reports"

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEVERITIES = ["critical", "error", "warning", "info"]
SEV_FROM_ICON = {"🔴": "error", "🟡": "warning", "🔵": "info", "⛔": "critical"}


# ══════════════════════════════════════════════════════════════
#  DATASET COMMIT + GIORNI
# ══════════════════════════════════════════════════════════════

def _day_dirs(reports_dir: Path) -> list:
    if not reports_dir.is_dir():
        return []
    return sorted(d for d in reports_dir.iterdir() if d.is_dir() and DAY_RE.match(d.name))


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _reviews_from_model(model: dict) -> dict:
    """{(repo, short_hash): review} dal dashboard-data.json di un giorno."""
    out = {}
    for repo in model.get("repos", []):
        for branch in repo.get("branches", []):
            for c in branch.get("commits", []):
                out[(repo["name"], c["short_hash"])] = {
                    "quality": c.get("quality_score"),
                    "summary": c.get("summary", ""),
                    "findings": c.get("findings", []),
                }
    return out


_COMMIT_LINE = re.compile(r"^\*\*`([0-9a-f]{6,40})`\*\*\s+—\s+.*?(?:—\s+(\d+)/10\b.*)?$")
_FINDING_LINE = re.compile(r"^\s+(\S+)\s+\*\*\[([A-Z0-9_-]+)\]\*\*\s+(.*)$")
_REPO_LINE = re.compile(r"^## 📦 (.+)$")


def _reviews_from_summary(path: Path) -> dict:
    """Fallback per i giorni senza dashboard-data.json: parsing del Markdown."""
    out = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    repo, current = None, None
    for line in lines:
        m = _REPO_LINE.match(line)
        if m:
            repo, current = m.group(1).strip(), None
            continue
        m = _COMMIT_LINE.match(line)
        if m and repo:
            current = {"quality": int(m.group(2)) if m.group(2) else None,
                       "summary": "", "findings": []}
            out[(repo, m.group(1))] = current
            continue
        if current is None:
            continue
        s = line.strip()
        if s.startswith("- 💡 _") and s.endswith("_"):
            current["summary"] = s[5:-1]
            continue
        m = _FINDING_LINE.match(line)
        if m:
            current["findings"].append({
                "rule_id": m.group(2), "severity": SEV_FROM_ICON.get(m.group(1), "info"),
                "file": "", "line": "", "message": m.group(3), "suggestion": "",
            })
        elif s.startswith("- File: `") and current["findings"]:
            current["findings"][-1]["file"] = s[9:].rstrip("`")
        elif s.startswith("- 💬 ") and current["findings"]:
            current["findings"][-1]["suggestion"] = s[4:]
    return out


def _worst(findings: list) -> str:
    sevs = [f.get("severity") for f in findings]
    for s in SEVERITIES:
        if s in sevs:
            return s
    return "clean"


def load_dataset(reports_dir: Path = REPORTS_DIR) -> dict:
    """
    Dataset completo per il portale.

    commits: un record per (repo, hash) — se un commit compare in più giorni
    (es. backfill) vince la versione revisionata più recente.
    days: un record per cartella giorno, con stato della raccolta e documenti.
    """
    commits: dict = {}
    days = []
    repos_meta: dict = {}

    for day_dir in _day_dirs(reports_dir):
        day = day_dir.name
        model = _read_json(day_dir / "dashboard-data.json")
        if model:
            reviews = _reviews_from_model(model)
        else:
            reviews = _reviews_from_summary(day_dir / "daily-summary.md")

        fetch_errors = []
        repo_names = []
        day_commits = 0
        for jp in sorted(day_dir.glob("*-commits.json")):
            data = _read_json(jp)
            if not data:
                continue
            name = data.get("name") or jp.name[:-len("-commits.json")]
            repo_names.append(name)
            if data.get("path"):
                repos_meta[name] = {"name": name, "path": data["path"]}
            if data.get("errors"):
                fetch_errors.extend(f"[{name}] {e.splitlines()[0]}" for e in data["errors"])

            for c in data.get("commits", []):
                day_commits += 1
                key = (name, c["short_hash"])
                rv = reviews.get(key)
                if rv is None:
                    for (rn, h), cand in reviews.items():
                        if rn == name and c["hash"].startswith(h):
                            rv = cand
                            break
                rec = {
                    "id": f"{name}:{c['hash']}",
                    "repo": name, "hash": c["hash"], "short": c["short_hash"],
                    "author": c.get("author", ""), "date": c.get("date", "")[:19],
                    "day": day, "message": c.get("message", ""),
                    "branch": c.get("branch", ""),
                    "files_changed": c.get("files_changed", 0),
                    "ins": c.get("insertions", 0), "del": c.get("deletions", 0),
                    "files": c.get("files", []),
                    "reviewed": rv is not None and (rv["quality"] is not None or bool(rv["findings"]) or bool(rv["summary"])),
                    "quality": rv["quality"] if rv else None,
                    "summary": rv["summary"] if rv else "",
                    "findings": rv["findings"] if rv else [],
                }
                rec["worst"] = _worst(rec["findings"])
                prev = commits.get(rec["id"])
                if prev is None or rec["reviewed"] or not prev["reviewed"]:
                    commits[rec["id"]] = rec

        days.append({
            "date": day,
            "commits": day_commits,
            "repos": repo_names,
            "fetch_ok": not fetch_errors,
            "errors": fetch_errors,
            "has_model": model is not None,
            "quality": (model or {}).get("quality_avg"),
            "docs": [n for n in ("digest.md", "briefing.md", "daily-summary.md", "dashboard.html")
                     if (day_dir / n).exists()],
        })

    # Quality del giorno anche per i giorni legacy (media degli score per-commit).
    by_day: dict = {}
    for c in commits.values():
        if c["quality"]:
            by_day.setdefault(c["day"], []).append(c["quality"])
    for d in days:
        if d["quality"] is None and by_day.get(d["date"]):
            q = by_day[d["date"]]
            d["quality"] = round(sum(q) / len(q), 1)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commits": sorted(commits.values(), key=lambda c: c["date"]),
        "days": days,
        "repos": sorted(repos_meta.values(), key=lambda r: r["name"]),
    }


def dataset_signature(reports_dir: Path = REPORTS_DIR) -> tuple:
    """Firma economica per invalidare la cache: mtime dei file rilevanti."""
    sig = []
    for d in _day_dirs(reports_dir):
        for p in d.iterdir():
            if p.suffix in (".json", ".md"):
                try:
                    sig.append((p.name, d.name, p.stat().st_mtime_ns))
                except OSError:
                    pass
    return tuple(sig)


def day_documents(day: str, reports_dir: Path = REPORTS_DIR) -> dict:
    """Markdown di un giorno (digest, briefing, summary) per la vista report."""
    if not DAY_RE.match(day):
        return {}
    day_dir = reports_dir / day
    docs = {}
    for name in ("digest.md", "briefing.md", "daily-summary.md"):
        p = day_dir / name
        if p.exists():
            docs[name] = p.read_text(encoding="utf-8", errors="replace")
    model = _read_json(day_dir / "dashboard-data.json")
    return {"date": day, "docs": docs, "model": model,
            "has_dashboard": (day_dir / "dashboard.html").exists()}


def commit_diff(repo: str, short_hash: str, reports_dir: Path = REPORTS_DIR) -> str:
    """Diff salvato al momento della raccolta (nessuna chiamata git)."""
    if not re.fullmatch(r"[0-9a-f]{6,40}", short_hash or ""):
        return ""
    safe_repo = Path(repo).name
    for day_dir in reversed(_day_dirs(reports_dir)):
        data = _read_json(day_dir / f"{safe_repo}-commits.json")
        if not data:
            continue
        for c in data.get("commits", []):
            if c["short_hash"] == short_hash or c["hash"].startswith(short_hash):
                return c.get("diff", "")
    return ""


# ══════════════════════════════════════════════════════════════
#  GIT-FLOW (topologia dai cloni locali)
# ══════════════════════════════════════════════════════════════

FAMILY_ORDER = ["main", "develop", "release", "hotfix", "feature", "bugfix", "other"]


def branch_family(name: str) -> str:
    n = (name or "").lower()
    if n.startswith("origin/"):
        n = n[7:]
    if n in ("main", "master"):
        return "main"
    if n in ("develop", "development", "dev"):
        return "develop"
    # Prima release/hotfix ovunque nel nome: es. "feature/release_8_11_0" è un release branch.
    for fam in ("release", "hotfix"):
        if n.startswith(fam) or f"/{fam}" in n:
            return fam
    for fam in ("feature", "bugfix"):
        if n.startswith(fam):
            return fam
    if n.startswith("fix"):
        return "bugfix"
    return "other"


def _git(path: str, args: list, timeout: int = 30) -> str:
    try:
        r = subprocess.run(["git", "-C", path, *args], capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _ref_priority(ref: str) -> tuple:
    return (FAMILY_ORDER.index(branch_family(ref)), ref)


def git_graph(repo_path: str, since: str, until: str = "", limit: int = 800) -> dict:
    """
    Layout a corsie del grafo dei commit nell'intervallo richiesto.

    Ogni commit riceve `col` (corsia) e `branch` (nome dedotto risalendo i
    first-parent a partire dalle ref, in ordine main → develop → release →
    hotfix → feature → bugfix). Gli archi verso parent fuori finestra
    scendono fino al fondo (`parent_row = None`).
    """
    if not repo_path or not Path(repo_path, ".git").exists():
        return {"error": "clone locale non trovato", "commits": [], "lanes": 0}

    args = ["log", "--all", "--date-order", f"--max-count={limit}",
            "--format=%H%x1f%P%x1f%an%x1f%aI%x1f%s%x1f%D%x1e"]
    if since:
        args.append(f"--since={since}T00:00:00")
    if until:
        args.append(f"--until={until}T23:59:59")
    out = _git(repo_path, args)

    rows = []
    for rec in out.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 6:
            continue
        h, parents, author, date, subject, refs = parts[:6]
        ref_list = [r.strip() for r in refs.split(",") if r.strip()]
        ref_list = [r.split(" -> ")[-1] for r in ref_list if r != "HEAD" and not r.startswith("HEAD ->")] \
            + [r.split(" -> ")[-1] for r in ref_list if r.startswith("HEAD ->")]
        rows.append({
            "hash": h, "short": h[:9], "parents": parents.split() if parents else [],
            "author": author, "date": date[:19], "subject": subject,
            "refs": [r for r in ref_list if r != "origin/HEAD"],
        })

    index = {r["hash"]: i for i, r in enumerate(rows)}

    # Nome del branch: propagazione first-parent dalle ref, in ordine di priorità.
    heads = []
    for i, r in enumerate(rows):
        for ref in r["refs"]:
            if ref.startswith("tag: "):
                continue
            heads.append((_ref_priority(ref), i, ref))
    heads.sort()
    for _, i, ref in heads:
        name = ref[7:] if ref.startswith("origin/") else ref
        j = i
        while j is not None and "branch" not in rows[j]:
            rows[j]["branch"] = name
            ps = rows[j]["parents"]
            j = index.get(ps[0]) if ps else None
    # Commit raggiungibili solo via merge: eredita il nome dal merge.
    for r in rows:
        if "branch" not in r:
            r["branch"] = ""
    for i, r in enumerate(rows):
        for p in r["parents"][1:]:
            j = index.get(p)
            while j is not None and not rows[j]["branch"]:
                rows[j]["branch"] = "(merged into " + (r["branch"] or "?") + ")"
                ps = rows[j]["parents"]
                j = index.get(ps[0]) if ps else None

    # Assegnazione corsie.
    lanes: list = []          # hash atteso per corsia, None = libera
    max_lanes = 0
    for i, r in enumerate(rows):
        h = r["hash"]
        cols = [k for k, v in enumerate(lanes) if v == h]
        if cols:
            col = cols[0]
            for k in cols[1:]:
                lanes[k] = None
        else:
            col = lanes.index(None) if None in lanes else len(lanes)
            if col == len(lanes):
                lanes.append(None)
        r["col"] = col
        edges = []
        for n, p in enumerate(r["parents"]):
            if n == 0:
                lanes[col] = p
                lane = col
            elif p in lanes:
                lane = lanes.index(p)
            else:
                lane = lanes.index(None) if None in lanes else len(lanes)
                if lane == len(lanes):
                    lanes.append(None)
                lanes[lane] = p
            edges.append({"parent": p, "lane": lane, "parent_row": index.get(p)})
        if not r["parents"]:
            lanes[col] = None
        r["edges"] = edges
        r["family"] = branch_family(r["branch"].replace("(merged into ", "").rstrip(")"))
        r["merge"] = len(r["parents"]) > 1
        r["tags"] = [ref[5:] for ref in r["refs"] if ref.startswith("tag: ")]
        while lanes and lanes[-1] is None:
            lanes.pop()
        max_lanes = max(max_lanes, len(lanes), col + 1)

    # Colonna finale del parent per disegnare l'ultimo tratto dell'arco.
    for r in rows:
        for e in r["edges"]:
            pr = e["parent_row"]
            e["parent_col"] = rows[pr]["col"] if pr is not None else None

    return {"commits": rows, "lanes": max_lanes, "truncated": len(rows) >= limit}


def git_show(repo_path: str, commit_hash: str, max_lines: int = 800) -> str:
    """Fallback quando il diff non è stato salvato: `git show` dal clone locale."""
    if not repo_path or not re.fullmatch(r"[0-9a-f]{6,40}", commit_hash or ""):
        return ""
    out = _git(repo_path, ["show", "--format=", "--no-color", commit_hash])
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (troncato: {len(lines) - max_lines} righe omesse)"]
    return "\n".join(lines)


def release_timeline(repo_path: str) -> list:
    """Tag con data e numero di commit introdotti rispetto al tag precedente."""
    if not repo_path or not Path(repo_path, ".git").exists():
        return []
    out = _git(repo_path, ["for-each-ref", "--sort=creatordate",
                           "--format=%(refname:short)%1f%(creatordate:iso-strict)", "refs/tags"])
    tags = []
    prev = None
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 2:
            continue
        name, date = parts
        count = None
        if prev:
            c = _git(repo_path, ["rev-list", "--count", f"{prev}..{name}"])
            count = int(c.strip()) if c.strip().isdigit() else None
        tags.append({"tag": name, "date": date[:19], "commits": count})
        prev = name
    return tags
