"""
review_db.py — Base dati locale delle review (SQLite)

Archivio unico di tutto ciò che producono le routine: commit raccolti,
esiti della review, finding, stato della raccolta per repo, documenti del
giorno (daily-summary, digest, briefing) e suggestion della KB. Il portale
legge solo da qui.

Come si alimenta:
- `daily_review.py` chiama `ingest_day()` a fine run (commit + review);
- `html_report.py --date` (passo dopo il digest) chiama `ingest_day()`;
- il portale chiama `sync()` a ogni richiesta del dataset: confronta le
  mtime dei file sotto `reports/` e `config/kb_suggestions/` e reimporta
  solo ciò che è cambiato, così anche i documenti scritti dagli agenti
  (es. il briefing del mattino) entrano senza hook dedicati.

I file sotto `reports/` restano la fonte: il database si ricostruisce da
zero con `--rebuild`. Ma è anche un archivio: un giorno importato resta nel
database anche se la sua cartella viene cancellata.

    python3 scripts/review_db.py --sync          # import incrementale
    python3 scripts/review_db.py --rebuild       # ricrea da zero
    python3 scripts/review_db.py --date 2026-09-18
    python3 scripts/review_db.py --stats

Percorso: `data/review.db` (override: $GDR_DB). Gitignored: contiene dati
del cliente.
"""

import argparse
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT_DIR / "reports"
SUGGESTIONS_DIR = ROOT_DIR / "config" / "kb_suggestions"
DB_PATH = Path(os.environ.get("GDR_DB") or ROOT_DIR / "data" / "review.db")
LEGACY_DASHBOARDS = REPORTS_DIR / ".portal" / "dashboards.json"

SCHEMA_VERSION = 3
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEVERITIES = ["critical", "error", "warning", "info"]
SEV_FROM_ICON = {"🔴": "error", "🟡": "warning", "🔵": "info", "⛔": "critical"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Una riga per cartella giorno importata.
CREATE TABLE IF NOT EXISTS days (
    date          TEXT PRIMARY KEY,
    generated_at  TEXT,
    commits       INTEGER NOT NULL DEFAULT 0,
    quality_avg   REAL,
    fetch_ok      INTEGER NOT NULL DEFAULT 1,
    errors_json   TEXT NOT NULL DEFAULT '[]',
    has_model     INTEGER NOT NULL DEFAULT 0,
    has_dashboard INTEGER NOT NULL DEFAULT 0,
    ingested_at   TEXT NOT NULL,
    covers_since  TEXT,                   -- intervallo raccolto (run di recupero: più giorni)
    covers_until  TEXT,
    collected_at  TEXT,
    catch_up_json TEXT                    -- run di recupero: giorni recuperati / in attesa
);
-- Esito della raccolta e sintesi AI per repository e giorno.
CREATE TABLE IF NOT EXISTS repo_runs (
    date             TEXT NOT NULL,
    repo             TEXT NOT NULL,
    path             TEXT,
    fetch_ok         INTEGER NOT NULL DEFAULT 1,
    errors_json      TEXT NOT NULL DEFAULT '[]',
    branches_scanned INTEGER,
    commits          INTEGER NOT NULL DEFAULT 0,
    quality_avg      REAL,
    summary          TEXT,
    positive_json    TEXT NOT NULL DEFAULT '[]',
    critical_json    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (date, repo)
);
CREATE TABLE IF NOT EXISTS repos (
    name TEXT PRIMARY KEY,
    path TEXT
);
-- Un commit per (repo, hash), anche se raccolto in più giorni.
CREATE TABLE IF NOT EXISTS commits (
    repo          TEXT NOT NULL,
    hash          TEXT NOT NULL,
    short         TEXT NOT NULL,
    author        TEXT,
    email         TEXT,
    date          TEXT,
    message       TEXT,
    branch        TEXT,
    files_changed INTEGER NOT NULL DEFAULT 0,
    ins           INTEGER NOT NULL DEFAULT 0,
    del           INTEGER NOT NULL DEFAULT 0,
    files_json    TEXT NOT NULL DEFAULT '[]',
    diff          TEXT,
    first_day     TEXT NOT NULL,
    last_day      TEXT NOT NULL,
    PRIMARY KEY (repo, hash)
);
CREATE INDEX IF NOT EXISTS commits_date ON commits(date);
CREATE INDEX IF NOT EXISTS commits_author ON commits(author);
-- Ultimo esito di review disponibile per il commit.
CREATE TABLE IF NOT EXISTS reviews (
    repo    TEXT NOT NULL,
    hash    TEXT NOT NULL,
    day     TEXT NOT NULL,
    quality INTEGER,
    summary TEXT,
    worst   TEXT NOT NULL,
    PRIMARY KEY (repo, hash)
);
CREATE TABLE IF NOT EXISTS findings (
    id         INTEGER PRIMARY KEY,
    repo       TEXT NOT NULL,
    hash       TEXT NOT NULL,
    day        TEXT NOT NULL,
    rule_id    TEXT,
    severity   TEXT NOT NULL,
    file       TEXT,
    line       TEXT,
    message    TEXT,
    suggestion TEXT
);
CREATE INDEX IF NOT EXISTS findings_commit ON findings(repo, hash);
CREATE INDEX IF NOT EXISTS findings_rule ON findings(rule_id);
-- Documenti Markdown del giorno: daily-summary, digest, briefing, …
CREATE TABLE IF NOT EXISTS documents (
    date    TEXT NOT NULL,
    name    TEXT NOT NULL,
    content TEXT NOT NULL,
    mtime   TEXT NOT NULL,
    PRIMARY KEY (date, name)
);
-- Briefing per il reparto sviluppo (uno per file: briefing.md + versioni).
CREATE TABLE IF NOT EXISTS briefings (
    date          TEXT NOT NULL,          -- giorno del digest su cui si basa
    name          TEXT NOT NULL,          -- briefing.md, briefing-2026-10-05.md, …
    prepared_on   TEXT,                   -- giorno in cui è stato preparato
    is_current    INTEGER NOT NULL,       -- 1 = briefing.md, 0 = versione precedente
    title         TEXT,
    intro         TEXT,
    sections_json TEXT NOT NULL DEFAULT '[]',
    open_question TEXT,
    PRIMARY KEY (date, name)
);
CREATE INDEX IF NOT EXISTS briefings_prepared ON briefings(prepared_on);
CREATE TABLE IF NOT EXISTS kb_suggestions (
    id              TEXT PRIMARY KEY,
    date            TEXT,
    source          TEXT,
    layer           TEXT,
    section         TEXT,
    type            TEXT,
    reason          TEXT,
    confidence      REAL,
    auto_approvable INTEGER,
    status          TEXT,
    repo            TEXT,
    reviewed_at     TEXT,
    merged_at       TEXT,
    content_json    TEXT
);
CREATE TABLE IF NOT EXISTS dashboards (
    id           TEXT PRIMARY KEY,
    position     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    widgets_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
-- Stato dell'import incrementale: mtime/size di ogni file sorgente.
CREATE TABLE IF NOT EXISTS sources (
    path     TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    size     INTEGER NOT NULL
);
"""

_write_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════
#  CONNESSIONE
# ══════════════════════════════════════════════════════════════

@contextmanager
def connect(db_path: Path = None):
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _migrate(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    conn.executescript(SCHEMA)
    if 0 < version < 3:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(days)")}
        for col in ("covers_since", "covers_until", "collected_at", "catch_up_json"):
            if col not in cols:
                conn.execute(f"ALTER TABLE days ADD COLUMN {col} TEXT")
    if version > 0:
        # Nuove tabelle derivate dai report: azzera lo stato dell'import
        # incrementale così il prossimo sync reimporta tutti i giorni.
        conn.execute("DELETE FROM sources")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    _bump(conn)


def _bump(conn):
    """Contatore di versione dei dati: il portale lo usa per la cache."""
    conn.execute(
        "INSERT INTO meta(key, value) VALUES ('data_version', '1') "
        "ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")


def data_version(db_path: Path = None) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()
        return int(row[0]) if row else 0


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════════════
#  PARSING DEI REPORT
# ══════════════════════════════════════════════════════════════

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


_BRIEFING_NAME = re.compile(r"^briefing(?:-prev)?(?:-(\d{4}-\d{2}-\d{2}))?\.md$")
_PREPARED = re.compile(r"Preparat[oa][^0-9\n]{0,60}?(\d{4}-\d{2}-\d{2})")
_DMY = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")
_YMD = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _question(body: str) -> str:
    """La domanda vera e propria: il primo blockquote, altrimenti il primo paragrafo."""
    blocks = re.split(r"\n\s*\n", body.strip())
    quote = next((b for b in blocks if b.lstrip().startswith(">")), None)
    return (quote or (blocks[0] if blocks else "")).strip()


def parse_briefing(name: str, content: str, mtime_day: str) -> dict:
    """
    Struttura di un briefing: titolo, introduzione (blockquote iniziale),
    sezioni `## …` e domanda aperta. La data di preparazione viene dal nome
    del file (`briefing-YYYY-MM-DD.md`), poi da "Preparato il …", poi dal
    titolo, infine dalla data di modifica del file.
    """
    lines = content.splitlines()
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), "")
    sections, intro, cur = [], [], None
    for l in lines:
        if l.startswith("## "):
            cur = {"title": l[3:].strip(), "body": []}
            sections.append(cur)
        elif re.match(r"^\s*---+\s*$", l) and cur is not None:
            # Un separatore chiude la sezione: ciò che segue (es. "Azione per
            # oggi") va in un blocco senza titolo, non nella domanda aperta.
            cur = {"title": "", "body": []}
            sections.append(cur)
        elif l.startswith("# ") and cur is None:
            continue
        elif cur is None:
            intro.append(l)
        else:
            cur["body"].append(l)
    for sec in sections:
        sec["body"] = "\n".join(sec["body"]).strip()
    sections = [sec for sec in sections if sec["title"] or sec["body"]]

    prepared = None
    m = _BRIEFING_NAME.match(name)
    if m and m.group(1):
        prepared = m.group(1)
    if not prepared:
        m = _PREPARED.search(content[:1500])
        prepared = m.group(1) if m else None
    if not prepared:
        m = _DMY.search(title)
        if m:
            prepared = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        elif "digest" not in title.lower():
            m = _YMD.search(title)
            prepared = m.group(1) if m else None
    question = _question(next((sec["body"] for sec in sections if "domanda" in sec["title"].lower()), ""))
    return {
        "title": title, "intro": "\n".join(intro).strip(), "sections": sections,
        "open_question": question, "prepared_on": prepared or mtime_day,
        "is_current": name == "briefing.md",
    }


def _worst(findings: list) -> str:
    sevs = {f.get("severity") for f in findings}
    for s in SEVERITIES:
        if s in sevs:
            return s
    return "clean"


def _has_review(rv) -> bool:
    return bool(rv) and (rv["quality"] is not None or bool(rv["findings"]) or bool(rv["summary"]))


# ══════════════════════════════════════════════════════════════
#  IMPORT
# ══════════════════════════════════════════════════════════════

def ingest_day(day: str, reports_dir: Path = REPORTS_DIR, db_path: Path = None) -> dict:
    """Importa (o reimporta) una cartella giorno. Idempotente."""
    if not DAY_RE.match(day):
        raise ValueError(f"data non valida: {day}")
    day_dir = Path(reports_dir) / day
    if not day_dir.is_dir():
        raise FileNotFoundError(f"{day_dir} non trovata")

    model = _read_json(day_dir / "dashboard-data.json")
    reviews = _reviews_from_model(model) if model else _reviews_from_summary(day_dir / "daily-summary.md")
    model_repos = {r["name"]: r for r in (model or {}).get("repos", [])}

    stats = {"day": day, "commits": 0, "reviews": 0, "findings": 0, "documents": 0}
    day_errors, day_scores, day_commits = [], [], 0
    cover = {"since": None, "until": None, "collected": None, "catch_up": None}

    with _write_lock, connect(db_path) as conn:
        for jp in sorted(day_dir.glob("*-commits.json")):
            data = _read_json(jp)
            if not data:
                continue
            name = data.get("name") or jp.name[:-len("-commits.json")]
            errors = [e.splitlines()[0] for e in data.get("errors", []) if e]
            cov = data.get("coverage") or {}
            if cov.get("since") and (cover["since"] is None or cov["since"] < cover["since"]):
                cover["since"] = cov["since"]
            if cov.get("until") and (cover["until"] is None or cov["until"] > cover["until"]):
                cover["until"] = cov["until"]
            cover["collected"] = data.get("collected_at") or cover["collected"]
            cover["catch_up"] = data.get("catch_up") or cover["catch_up"]
            day_errors.extend(f"[{name}] {e}" for e in errors)
            if data.get("path"):
                conn.execute("INSERT INTO repos(name, path) VALUES (?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET path = excluded.path",
                             (name, data["path"]))

            repo_scores = []
            commits = data.get("commits", [])
            for c in commits:
                day_commits += 1
                stats["commits"] += 1
                diff = c.get("diff") or None
                conn.execute("""
                    INSERT INTO commits(repo, hash, short, author, email, date, message, branch,
                                        files_changed, ins, del, files_json, diff, first_day, last_day)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(repo, hash) DO UPDATE SET
                        short = excluded.short, author = excluded.author, email = excluded.email,
                        date = excluded.date, message = excluded.message, branch = excluded.branch,
                        files_changed = excluded.files_changed, ins = excluded.ins, del = excluded.del,
                        files_json = excluded.files_json,
                        diff = COALESCE(excluded.diff, commits.diff),
                        first_day = MIN(commits.first_day, excluded.first_day),
                        last_day = MAX(commits.last_day, excluded.last_day)
                """, (name, c["hash"], c["short_hash"], c.get("author", ""), c.get("email", ""),
                      c.get("date", "")[:19], c.get("message", ""), c.get("branch", ""),
                      c.get("files_changed", 0), c.get("insertions", 0), c.get("deletions", 0),
                      json.dumps(c.get("files", []), ensure_ascii=False), diff, day, day))

                rv = reviews.get((name, c["short_hash"]))
                if rv is None:
                    rv = next((v for (rn, h), v in reviews.items()
                               if rn == name and c["hash"].startswith(h)), None)
                if not _has_review(rv):
                    continue
                # Una review più vecchia non sovrascrive una più recente.
                prev = conn.execute("SELECT day FROM reviews WHERE repo=? AND hash=?",
                                    (name, c["hash"])).fetchone()
                if prev and prev["day"] > day:
                    continue
                findings = [f for f in rv["findings"] if isinstance(f, dict)]
                for f in findings:
                    if f.get("severity") not in SEVERITIES:
                        f["severity"] = "info"
                conn.execute("""
                    INSERT INTO reviews(repo, hash, day, quality, summary, worst) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(repo, hash) DO UPDATE SET day = excluded.day, quality = excluded.quality,
                        summary = excluded.summary, worst = excluded.worst
                """, (name, c["hash"], day, rv["quality"], rv["summary"], _worst(findings)))
                conn.execute("DELETE FROM findings WHERE repo=? AND hash=?", (name, c["hash"]))
                conn.executemany("""
                    INSERT INTO findings(repo, hash, day, rule_id, severity, file, line, message, suggestion)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, [(name, c["hash"], day, f.get("rule_id", ""), f["severity"], f.get("file", ""),
                       str(f.get("line", "") or ""), f.get("message", ""), f.get("suggestion", ""))
                      for f in findings])
                stats["reviews"] += 1
                stats["findings"] += len(findings)
                if rv["quality"]:
                    repo_scores.append(rv["quality"])
                    day_scores.append(rv["quality"])

            mr = model_repos.get(name, {})
            conn.execute("""
                INSERT OR REPLACE INTO repo_runs(date, repo, path, fetch_ok, errors_json, branches_scanned,
                    commits, quality_avg, summary, positive_json, critical_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (day, name, data.get("path"), 0 if errors else 1, json.dumps(errors, ensure_ascii=False),
                  len(data.get("branches_scanned", [])), len(commits),
                  mr.get("quality_avg") or (round(sum(repo_scores) / len(repo_scores), 1) if repo_scores else None),
                  mr.get("summary", ""), json.dumps(mr.get("positive_notes", []), ensure_ascii=False),
                  json.dumps(mr.get("critical_issues", []), ensure_ascii=False)))

        conn.execute("DELETE FROM briefings WHERE date=?", (day,))
        for md in sorted(day_dir.glob("*.md")):
            try:
                content = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            mtime = datetime.fromtimestamp(md.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("INSERT OR REPLACE INTO documents(date, name, content, mtime) VALUES (?,?,?,?)",
                         (day, md.name, content, mtime))
            stats["documents"] += 1
            if _BRIEFING_NAME.match(md.name):
                b = parse_briefing(md.name, content, mtime[:10])
                conn.execute("""
                    INSERT OR REPLACE INTO briefings(date, name, prepared_on, is_current, title, intro,
                        sections_json, open_question) VALUES (?,?,?,?,?,?,?,?)
                """, (day, md.name, b["prepared_on"], 1 if b["is_current"] else 0, b["title"], b["intro"],
                      json.dumps(b["sections"], ensure_ascii=False), b["open_question"]))
                stats["briefings"] = stats.get("briefings", 0) + 1

        quality = (model or {}).get("quality_avg")
        if quality is None and day_scores:
            quality = round(sum(day_scores) / len(day_scores), 1)
        conn.execute("""
            INSERT OR REPLACE INTO days(date, generated_at, commits, quality_avg, fetch_ok, errors_json,
                has_model, has_dashboard, ingested_at, covers_since, covers_until, collected_at, catch_up_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (day, (model or {}).get("generated_at"), day_commits, quality, 0 if day_errors else 1,
              json.dumps(day_errors, ensure_ascii=False), 1 if model else 0,
              1 if (day_dir / "dashboard.html").exists() else 0, _now(),
              cover["since"], cover["until"], cover["collected"],
              json.dumps(cover["catch_up"], ensure_ascii=False) if cover["catch_up"] else None))

        _record_sources(conn, _day_files(day_dir))
        _bump(conn)
    return stats


def ingest_kb(suggestions_dir: Path = SUGGESTIONS_DIR, db_path: Path = None) -> int:
    """Importa le suggestion KB (tutti gli stati). Richiede PyYAML."""
    if not Path(suggestions_dir).is_dir():
        return 0
    try:
        import yaml
    except ImportError:
        return 0
    files = sorted(Path(suggestions_dir).glob("*.yaml"))
    n = 0
    with _write_lock, connect(db_path) as conn:
        for p in files:
            try:
                d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except Exception:
                continue
            if not d.get("id"):
                continue
            conn.execute("""
                INSERT OR REPLACE INTO kb_suggestions(id, date, source, layer, section, type, reason,
                    confidence, auto_approvable, status, repo, reviewed_at, merged_at, content_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (d["id"], str(d.get("date") or ""), d.get("source"), d.get("layer"), d.get("section"),
                  d.get("type"), d.get("reason"), d.get("confidence"),
                  1 if d.get("auto_approvable") else 0, d.get("status"), d.get("repo"),
                  str(d.get("reviewed_at") or "") or None, str(d.get("merged_at") or "") or None,
                  json.dumps(d.get("content"), ensure_ascii=False, default=str)))
            n += 1
        _record_sources(conn, files)
        _bump(conn)
    return n


def _day_files(day_dir: Path) -> list:
    return [p for p in day_dir.iterdir() if p.is_file() and p.suffix in (".json", ".md", ".html")]


def _record_sources(conn, paths):
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        conn.execute("INSERT OR REPLACE INTO sources(path, mtime_ns, size) VALUES (?,?,?)",
                     (_rel(p), st.st_mtime_ns, st.st_size))


def _rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT_DIR))
    except ValueError:
        return str(Path(p).resolve())


def _changed(conn, paths) -> bool:
    known = {r["path"]: (r["mtime_ns"], r["size"]) for r in conn.execute("SELECT * FROM sources")}
    for p in paths:
        try:
            st = p.stat()
        except OSError:
            continue
        if known.get(_rel(p)) != (st.st_mtime_ns, st.st_size):
            return True
    return False


def sync(reports_dir: Path = REPORTS_DIR, suggestions_dir: Path = SUGGESTIONS_DIR,
         db_path: Path = None, full: bool = False) -> dict:
    """Import incrementale: solo i giorni e le suggestion con file cambiati."""
    reports_dir = Path(reports_dir)
    day_dirs = sorted(d for d in reports_dir.iterdir() if d.is_dir() and DAY_RE.match(d.name)) \
        if reports_dir.is_dir() else []
    with connect(db_path) as conn:
        todo = [d.name for d in day_dirs if full or _changed(conn, _day_files(d))]
        kb_files = sorted(Path(suggestions_dir).glob("*.yaml")) if Path(suggestions_dir).is_dir() else []
        kb_todo = full or _changed(conn, kb_files)
        migrate_dash = not conn.execute("SELECT 1 FROM dashboards LIMIT 1").fetchone() \
            and LEGACY_DASHBOARDS.exists()
    result = {"days": [], "kb": 0}
    for day in todo:
        ingest_day(day, reports_dir, db_path)
        result["days"].append(day)
    if kb_todo:
        result["kb"] = ingest_kb(suggestions_dir, db_path)
    if migrate_dash:
        legacy = _read_json(LEGACY_DASHBOARDS)
        if isinstance(legacy, list):
            save_dashboards(legacy, db_path)
    return result


def rebuild(reports_dir: Path = REPORTS_DIR, db_path: Path = None) -> dict:
    """Ricrea il database da zero (le dashboard personalizzate vengono preservate)."""
    path = Path(db_path or DB_PATH)
    dashboards = load_dashboards(db_path) if path.exists() else []
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    if dashboards:
        save_dashboards(dashboards, db_path)
    return sync(reports_dir, db_path=db_path, full=True)


# ══════════════════════════════════════════════════════════════
#  QUERY PER IL PORTALE
# ══════════════════════════════════════════════════════════════

def load_dataset(db_path: Path = None) -> dict:
    """Dataset completo a livello di commit (stessa forma attesa dal portale)."""
    with connect(db_path) as conn:
        findings: dict = {}
        for f in conn.execute("SELECT * FROM findings ORDER BY id"):
            findings.setdefault((f["repo"], f["hash"]), []).append({
                "rule_id": f["rule_id"], "severity": f["severity"], "file": f["file"],
                "line": f["line"], "message": f["message"], "suggestion": f["suggestion"]})
        commits = []
        for r in conn.execute("""
            SELECT c.*, rv.day AS rv_day, rv.quality, rv.summary, rv.worst
            FROM commits c LEFT JOIN reviews rv ON rv.repo = c.repo AND rv.hash = c.hash
            ORDER BY c.date
        """):
            reviewed = r["rv_day"] is not None
            commits.append({
                "id": f"{r['repo']}:{r['hash']}", "repo": r["repo"], "hash": r["hash"],
                "short": r["short"], "author": r["author"] or "", "date": r["date"] or "",
                "day": r["rv_day"] or r["last_day"], "message": r["message"] or "",
                "branch": r["branch"] or "", "files_changed": r["files_changed"],
                "ins": r["ins"], "del": r["del"], "files": json.loads(r["files_json"] or "[]"),
                "reviewed": reviewed, "quality": r["quality"], "summary": r["summary"] or "",
                "findings": findings.get((r["repo"], r["hash"]), []),
                "worst": r["worst"] if reviewed else "clean",
            })
        docs: dict = {}
        for d in conn.execute("SELECT date, name FROM documents"):
            docs.setdefault(d["date"], []).append(d["name"])
        repos_by_day: dict = {}
        for rr in conn.execute("SELECT date, repo FROM repo_runs ORDER BY repo"):
            repos_by_day.setdefault(rr["date"], []).append(rr["repo"])
        days = []
        for d in conn.execute("SELECT * FROM days ORDER BY date"):
            names = docs.get(d["date"], [])
            days.append({
                "date": d["date"], "commits": d["commits"], "repos": repos_by_day.get(d["date"], []),
                "fetch_ok": bool(d["fetch_ok"]), "errors": json.loads(d["errors_json"]),
                "has_model": bool(d["has_model"]), "quality": d["quality_avg"],
                "docs": [n for n in ("digest.md", "briefing.md", "daily-summary.md") if n in names]
                        + (["dashboard.html"] if d["has_dashboard"] else []),
                "covers_since": d["covers_since"], "covers_until": d["covers_until"],
                "catch_up": json.loads(d["catch_up_json"]) if d["catch_up_json"] else None,
            })
        repos = [{"name": r["name"], "path": r["path"]}
                 for r in conn.execute("SELECT * FROM repos ORDER BY name")]
    return {"generated_at": _now(), "commits": commits, "days": days, "repos": repos}


def day_documents(day: str, db_path: Path = None) -> dict:
    if not DAY_RE.match(day or ""):
        return {}
    with connect(db_path) as conn:
        docs = {r["name"]: r["content"] for r in
                conn.execute("SELECT name, content FROM documents WHERE date=? ORDER BY name", (day,))}
        row = conn.execute("SELECT has_dashboard FROM days WHERE date=?", (day,)).fetchone()
        runs = [dict(r) for r in conn.execute("SELECT * FROM repo_runs WHERE date=? ORDER BY repo", (day,))]
    for r in runs:
        for k in ("errors_json", "positive_json", "critical_json"):
            r[k[:-5]] = json.loads(r.pop(k) or "[]")
    return {"date": day, "docs": docs, "repo_runs": runs,
            "has_dashboard": bool(row and row["has_dashboard"])}


def commit_diff(repo: str, short_hash: str, db_path: Path = None) -> str:
    if not re.fullmatch(r"[0-9a-f]{6,40}", short_hash or ""):
        return ""
    with connect(db_path) as conn:
        row = conn.execute("SELECT diff FROM commits WHERE repo=? AND hash LIKE ? LIMIT 1",
                           (repo, short_hash + "%")).fetchone()
    return (row["diff"] or "") if row else ""


def repo_path(name: str, db_path: Path = None):
    with connect(db_path) as conn:
        row = conn.execute("SELECT path FROM repos WHERE name=?", (name,)).fetchone()
    return row["path"] if row else None


def briefings(db_path: Path = None) -> list:
    """Tutti i briefing (con il testo completo), il più recente per primo."""
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT b.*, d.content FROM briefings b
            JOIN documents d ON d.date = b.date AND d.name = b.name
            ORDER BY b.prepared_on DESC, b.date DESC, b.is_current DESC, b.name DESC
        """)]
    for r in rows:
        r["sections"] = json.loads(r.pop("sections_json") or "[]")
        r["is_current"] = bool(r["is_current"])
    return rows


def kb_suggestions(db_path: Path = None) -> list:
    with connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM kb_suggestions ORDER BY id")]
    for r in rows:
        r["content"] = json.loads(r.pop("content_json") or "null")
        r["auto_approvable"] = bool(r["auto_approvable"])
    return rows


def load_dashboards(db_path: Path = None) -> list:
    with connect(db_path) as conn:
        return [{"id": r["id"], "name": r["name"], "widgets": json.loads(r["widgets_json"])}
                for r in conn.execute("SELECT * FROM dashboards ORDER BY position")]


def save_dashboards(dashboards: list, db_path: Path = None):
    with _write_lock, connect(db_path) as conn:
        conn.execute("DELETE FROM dashboards")
        for i, d in enumerate(dashboards):
            if not isinstance(d, dict) or not d.get("id"):
                continue
            conn.execute("INSERT INTO dashboards(id, position, name, widgets_json, updated_at) VALUES (?,?,?,?,?)",
                         (str(d["id"]), i, str(d.get("name") or "Dashboard"),
                          json.dumps(d.get("widgets", []), ensure_ascii=False), _now()))


def stats(db_path: Path = None) -> dict:
    with connect(db_path) as conn:
        count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
        span = conn.execute("SELECT MIN(date), MAX(date) FROM days").fetchone()
        return {
            "db": str(db_path or DB_PATH), "days": count("days"), "from": span[0], "to": span[1],
            "repos": count("repos"), "commits": count("commits"), "reviews": count("reviews"),
            "findings": count("findings"), "documents": count("documents"),
            "briefings": count("briefings"),
            "kb_suggestions": count("kb_suggestions"), "dashboards": count("dashboards"),
        }


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(description="Base dati locale delle daily review")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--sync", action="store_true", help="import incrementale (default)")
    g.add_argument("--rebuild", action="store_true", help="ricrea il database da zero")
    g.add_argument("--date", help="reimporta un giorno (YYYY-MM-DD)")
    g.add_argument("--stats", action="store_true", help="mostra i conteggi")
    args = ap.parse_args()

    if args.stats:
        for k, v in stats().items():
            print(f"  {k:<15} {v}")
        return
    if args.date:
        s = ingest_day(args.date)
        print(f"✓ {s['day']}: {s['commits']} commit, {s['reviews']} review, "
              f"{s['findings']} finding, {s['documents']} documenti")
        return
    res = rebuild() if args.rebuild else sync()
    print(f"✓ Giorni importati: {len(res['days'])}"
          + (f" ({res['days'][0]} … {res['days'][-1]})" if res["days"] else "")
          + f" · suggestion KB: {res['kb']}")
    print(f"  Database: {DB_PATH}")


if __name__ == "__main__":
    main()
