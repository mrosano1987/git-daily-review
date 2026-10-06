"""
portal_data.py — Dati git del portale web delle review

Topologia letta dai cloni locali (`git log --all`, mai `fetch`) per il
git-flow, timeline dei rilasci (tag) e `git show` come fallback quando il
diff non è stato salvato. I dati delle review stanno in `review_db.py`.

Tutto in sola lettura sui repository.
"""

import re
import subprocess
from pathlib import Path


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
