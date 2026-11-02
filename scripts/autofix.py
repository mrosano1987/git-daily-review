"""
autofix.py — Fix autonomi dei finding della review, con revisione del bot e PR

Il motore segnala i problemi; questo modulo li porta fino a una pull request
che resta da rivedere e da unire a mano. Chi decide *se* e *come* correggere
è l'agente (Claude, nella routine pianificata): qui ci sono solo i passaggi
deterministici e i controlli che non si possono saltare.

Ciclo di vita di un candidato (un commit con finding correggibili):

    candidates  → elenca i commit con finding critical/error ancora da trattare
    prepare ID  → worktree isolato su un branch `autofix/…` dal branch del commit
    (l'agente modifica il codice nel worktree e committa)
    check ID    → diff contenuto, nessun file sensibile, check del repo
    review ID   → revisione del bot del progetto: serve `VERDETTO: APPROVATO`
    publish ID  → push del solo branch autofix e apertura della PR (mai merge)
    skip / abandon ID → chiusura motivata, senza PR
    report      → riepilogo del giorno in reports/<data>/autofix.md

`publish` è l'unico punto in cui qualcosa esce dalla macchina e rifiuta di
procedere se l'ultima revisione approvata e i check non riguardano
esattamente il commit in testa al branch.

Configurazione: routine/autofix.yaml (modello: routine/autofix.example.yaml).
Stato: data/autofix.json. Entrambi gitignored: contengono dati del cliente.

Uso:
    python3 scripts/autofix.py candidates [--date D] [--json]
    python3 scripts/autofix.py prepare <id>
    python3 scripts/autofix.py check <id>
    python3 scripts/autofix.py review <id> --rationale "perché e come"
    python3 scripts/autofix.py publish <id> [--dry-run]
    python3 scripts/autofix.py skip <id> --reason "…"
    python3 scripts/autofix.py abandon <id> --reason "…"
    python3 scripts/autofix.py report [--date D]
    python3 scripts/autofix.py status
    python3 scripts/autofix.py cleanup [--days N]
"""

import argparse
import base64
import fnmatch
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap
    _bootstrap.ensure_deps()

import yaml  # noqa: E402

import review_db  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("GDR_AUTOFIX_CONFIG") or ROOT_DIR / "routine" / "autofix.yaml")
STATE_PATH = Path(os.environ.get("GDR_AUTOFIX_STATE") or ROOT_DIR / "data" / "autofix.json")
REPORTS_DIR = Path(os.environ.get("GDR_REPORTS_DIR") or ROOT_DIR / "reports")

DEFAULTS = {
    "enabled": True,
    "severities": ["critical", "error"],
    "skip_rules": ["GIT-*"],
    "lookback_days": 3,
    "max_prs_per_day": 2,
    "max_review_rounds": 2,
    "max_files": 5,
    "max_lines": 150,
    "branch_prefix": "autofix/",
    "worktrees_dir": str(ROOT_DIR / ".autofix" / "worktrees"),
    "protected_branches": ["main", "master", "develop", "release/*"],
    "forbidden_paths": [
        ".env", ".env.*", "*.env", "*.pem", "*.key", "*.p12", "*.jks", "*.keystore",
        "*.mobileprovision", "google-services.json", "GoogleService-Info.plist",
        "*secret*", "*credential*", "package-lock.json", "yarn.lock",
    ],
    "reviewer": {
        "name": "bot",
        "command": ["pa", "bot", "ask", "--as", "dev", "--show", "--profile", "{profile}"],
        "timeout": 1200,
        "max_diff_chars": 14000,
    },
    "repos": {},
}

TERMINAL = {"published", "pushed", "skipped", "abandoned"}
VERDICT_RX = re.compile(r"VERDETTO\s*[*_]*\s*[:：]\s*[*_]*\s*(APPROVATO|MODIFICHE|RESPINTO)", re.I)
LOW_CONFIDENCE_RX = re.compile(r"confidenza\s*[*_:\-–—]*\s*bassa", re.I)
HEADER_RX = re.compile(r"^\[(\w+)(?: #(\d+))?")


class AutofixError(Exception):
    """Errore atteso: il comando si ferma con un messaggio, senza traceback."""


# ══════════════════════════════════════════════════════════════
#  CONFIGURAZIONE E STATO
# ══════════════════════════════════════════════════════════════

def load_config(path: Path = None) -> dict:
    path = Path(path or CONFIG_PATH)
    if not path.is_file():
        raise AutofixError(f"Configurazione assente: {path}. Copia routine/autofix.example.yaml e adattala.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = {**DEFAULTS, **data}
    cfg["reviewer"] = {**DEFAULTS["reviewer"], **(data.get("reviewer") or {})}
    cfg["worktrees_dir"] = str(ROOT_DIR / os.path.expanduser(cfg["worktrees_dir"]))   # relativo alla root
    cfg["repos"] = cfg.get("repos") or {}
    return cfg


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"attempts": {}}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _attempt(state: dict, cid: str) -> dict:
    att = state["attempts"].get(cid)
    if not att:
        raise AutofixError(f"Candidato {cid} non preparato: esegui prima `prepare {cid}`.")
    return att


def _log(att: dict, event: str, **data):
    att.setdefault("events", []).append({"at": _now(), "event": event, **data})


# ══════════════════════════════════════════════════════════════
#  GIT
# ══════════════════════════════════════════════════════════════

def git(cwd, *args, check=True, timeout=300) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AutofixError(f"git {' '.join(args)} fallito: {(r.stderr or r.stdout).strip()[:400]}")
    return r.stdout.strip()


def _ref_exists(cwd, ref: str) -> bool:
    return subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=str(cwd),
                          capture_output=True).returncode == 0


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns or [])


def _is_protected(branch: str, cfg: dict, repo_cfg: dict) -> bool:
    return _matches(branch, repo_cfg.get("protected_branches") or cfg["protected_branches"])


def _forbidden(path: str, patterns) -> bool:
    name = path.rsplit("/", 1)[-1]
    return _matches(path, patterns) or _matches(name, patterns)


# ══════════════════════════════════════════════════════════════
#  CANDIDATI
# ══════════════════════════════════════════════════════════════

def candidate_id(repo: str, hash_: str) -> str:
    return f"{repo}@{hash_[:9]}"


def find_candidates(cfg: dict, day: str, state: dict = None, db_path: Path = None) -> list:
    """Commit con finding delle severità configurate, nella finestra di giorni, non ancora trattati."""
    state = state if state is not None else load_state()
    since = (date.fromisoformat(day) - timedelta(days=int(cfg["lookback_days"]))).isoformat()
    sev = list(cfg["severities"])
    marks = ",".join("?" * len(sev))
    with review_db.connect(db_path) as conn:
        rows = [dict(r) for r in conn.execute(f"""
            SELECT f.repo, f.hash, f.day, f.rule_id, f.severity, f.file, f.line, f.message, f.suggestion,
                   c.short, c.branch, c.author, c.message AS commit_message, r.path
              FROM findings f
              JOIN commits c ON c.repo = f.repo AND c.hash = f.hash
              LEFT JOIN repos r ON r.name = f.repo
             WHERE f.severity IN ({marks}) AND f.day BETWEEN ? AND ?
             ORDER BY f.day DESC, f.repo, f.hash""", (*sev, since, day))]
    out = {}
    for r in rows:
        if r["repo"] not in cfg["repos"] or _matches(r["rule_id"] or "", cfg["skip_rules"]):
            continue
        cid = candidate_id(r["repo"], r["hash"])
        if state["attempts"].get(cid, {}).get("status") in TERMINAL:
            continue
        repo_cfg = cfg["repos"][r["repo"]] or {}
        c = out.setdefault(cid, {
            "id": cid, "repo": r["repo"], "hash": r["hash"], "short": r["short"], "day": r["day"],
            "branch": r["branch"], "author": r["author"], "commit_message": r["commit_message"],
            "path": os.path.expanduser(repo_cfg.get("path") or r["path"] or ""),
            "reviewer_profile": repo_cfg.get("reviewer_profile"),
            "status": state["attempts"].get(cid, {}).get("status", "new"),
            "findings": [],
        })
        c["findings"].append({k: r[k] for k in ("rule_id", "severity", "file", "line", "message", "suggestion")})
    rank = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    return sorted(out.values(), key=lambda c: (min(rank.get(f["severity"], 9) for f in c["findings"]), c["day"]))


def _candidate(cfg: dict, cid: str, day: str) -> dict:
    for c in find_candidates(cfg, day, state={"attempts": {}}):
        if c["id"] == cid:
            return c
    raise AutofixError(f"Candidato {cid} non trovato tra i finding degli ultimi {cfg['lookback_days']} giorni.")


# ══════════════════════════════════════════════════════════════
#  PREPARE / CHECK
# ══════════════════════════════════════════════════════════════

def prepare(cfg: dict, cid: str, day: str) -> dict:
    state = load_state()
    att = state["attempts"].get(cid)
    if att and att["status"] in TERMINAL:
        raise AutofixError(f"{cid} è già chiuso ({att['status']}).")
    if att and Path(att["worktree"]).is_dir():
        return att                                    # ripresa di un tentativo interrotto

    c = _candidate(cfg, cid, day)
    repo = Path(c["path"])
    if not (repo / ".git").exists():
        raise AutofixError(f"Repository locale non trovato per {c['repo']}: {repo}")
    base = c["branch"] or ""
    git(repo, "fetch", "--quiet", "origin", timeout=600)
    if not base or not _ref_exists(repo, f"origin/{base}"):
        raise AutofixError(f"Il branch del commit ({base or '?'}) non esiste su origin: serve una decisione umana.")
    if not _ref_exists(repo, c["hash"]):
        raise AutofixError(f"Il commit {c['short']} non è presente nel repository locale.")
    contained = subprocess.run(["git", "merge-base", "--is-ancestor", c["hash"], f"origin/{base}"],
                               cwd=str(repo)).returncode == 0

    branch = att["branch"] if att else f"{cfg['branch_prefix']}{day}-{c['hash'][:9]}"
    worktree = Path(cfg["worktrees_dir"]) / c["repo"] / branch.replace("/", "-")
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if _ref_exists(repo, f"refs/heads/{branch}"):
        # branch già creato (worktree rimosso a metà tentativo): la base resta quella registrata
        git(repo, "worktree", "add", str(worktree), branch)
        base_sha = att["base_sha"] if att else git(repo, "merge-base", branch, f"origin/{base}")
    else:
        git(repo, "worktree", "add", "-b", branch, str(worktree), f"origin/{base}")
        base_sha = git(worktree, "rev-parse", "HEAD")

    att = {
        "reviews": [], **(att or {}),
        "id": cid, "repo": c["repo"], "repo_path": str(repo), "hash": c["hash"], "day": c["day"],
        "run_day": (att or {}).get("run_day", day), "findings": c["findings"],
        "commit_message": c["commit_message"], "author": c["author"], "base_branch": base,
        "base_sha": base_sha, "commit_in_base": contained, "branch": branch, "worktree": str(worktree),
        "reviewer_profile": c["reviewer_profile"], "status": "prepared",
    }
    _log(att, "prepared")
    state["attempts"][cid] = att
    save_state(state)
    return att


def _diff_stats(att: dict) -> dict:
    wt = att["worktree"]
    files, lines = [], 0
    for row in git(wt, "diff", "--numstat", f"{att['base_sha']}..HEAD").splitlines():
        added, removed, path = row.split("\t", 2)
        files.append(path)
        lines += (int(added) if added.isdigit() else 0) + (int(removed) if removed.isdigit() else 0)
    return {"files": files, "lines": lines,
            "commits": int(git(wt, "rev-list", "--count", f"{att['base_sha']}..HEAD") or 0)}


def check(cfg: dict, cid: str) -> dict:
    state = load_state()
    att = _attempt(state, cid)
    if att["status"] in TERMINAL:
        raise AutofixError(f"{cid} è già chiuso ({att['status']}).")
    wt = att["worktree"]
    repo_cfg = cfg["repos"].get(att["repo"]) or {}
    problems = []
    if git(wt, "status", "--porcelain"):
        problems.append("ci sono modifiche non committate nel worktree")
    stats = _diff_stats(att)
    if stats["commits"] == 0:
        problems.append("nessun commit sopra il branch di partenza")
    if len(stats["files"]) > int(repo_cfg.get("max_files", cfg["max_files"])):
        problems.append(f"troppi file modificati ({len(stats['files'])})")
    if stats["lines"] > int(repo_cfg.get("max_lines", cfg["max_lines"])):
        problems.append(f"diff troppo esteso ({stats['lines']} righe)")
    forbidden = [f for f in stats["files"] if _forbidden(f, cfg["forbidden_paths"])]
    if forbidden:
        problems.append("file che l'agente non può toccare: " + ", ".join(forbidden))

    results = []
    if not problems:
        for cmd in repo_cfg.get("checks") or []:
            argv = [a.replace("{repo}", att["repo_path"]).replace("{worktree}", wt) for a in cmd]
            try:
                r = subprocess.run(argv, cwd=wt, capture_output=True, text=True,
                                   timeout=int(repo_cfg.get("check_timeout", 900)))
                ok, tail = r.returncode == 0, (r.stdout + r.stderr)[-1500:]
            except (OSError, subprocess.TimeoutExpired) as e:
                ok, tail = False, str(e)
            results.append({"command": " ".join(argv), "ok": ok, "output_tail": tail})
            if not ok:
                problems.append(f"check fallito: {' '.join(argv)}")

    head = git(wt, "rev-parse", "HEAD")
    att["check"] = {"sha": head, "ok": not problems, "problems": problems, "stats": stats, "results": results}
    att["status"] = "checked" if not problems else "check_failed"
    _log(att, "check", sha=head, ok=not problems)
    save_state(state)
    return att["check"]


# ══════════════════════════════════════════════════════════════
#  REVIEW DEL BOT
# ══════════════════════════════════════════════════════════════

def build_question(att: dict, diff: str, rationale: str, max_chars: int) -> str:
    """Domanda per il bot. Il bot riceve il testo su una riga sola: le righe del diff si separano con ¶."""
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n[… diff troncato …]"
    findings = "; ".join(
        f"[{f['severity']}] {f['rule_id']} in {f['file']}:{f['line']} — {f['message']}"
        + (f" (suggerimento: {f['suggestion']})" if f.get("suggestion") else "")
        for f in att["findings"])
    diff_line = " ¶ ".join(diff.splitlines())
    return (
        f"Revisione tecnica di una correzione proposta in automatico sul repository {att['repo']}, "
        f"branch {att['base_branch']}. La review giornaliera ha segnalato sul commit {att['hash'][:9]} "
        f"(«{(att.get('commit_message') or '').splitlines()[0] if att.get('commit_message') else ''}»): {findings}. "
        f"Motivazione della correzione: {rationale}. "
        f"Diff della correzione (formato unified; ogni riga del diff è separata dal simbolo ¶): {diff_line} . "
        "Il checkout locale del repository può trovarsi su un altro branch: per la modifica fa fede il diff, "
        "per il contesto leggi i file citati nel codice. Valuta se la correzione risolve davvero il problema, "
        "se è minima e coerente con le convenzioni del progetto, se introduce regressioni o effetti su altre "
        "parti dell'app. Indica rischi e test da fare. Chiudi la risposta con una riga esatta: "
        "VERDETTO: APPROVATO oppure VERDETTO: MODIFICHE (con le modifiche richieste) oppure VERDETTO: RESPINTO (con il motivo)."
    )


def parse_review(output: str) -> dict:
    """Esito della revisione: approvata solo con risposta completa, verdetto esplicito e confidenza non bassa."""
    header = HEADER_RX.match(output.strip())
    status = header.group(1).lower() if header else "unknown"
    request_id = header.group(2) if header else None
    body = output.split("━━", 2)[-1] if "━━ BOZZA" in output else output
    verdicts = VERDICT_RX.findall(body)
    verdict = verdicts[-1].upper() if verdicts else None
    approved, reason = False, ""
    if status not in ("answered", "draft"):
        reason = f"il bot non ha dato una risposta completa (stato: {status})"
    elif not verdict:
        reason = "nessun verdetto esplicito nella risposta"
    elif LOW_CONFIDENCE_RX.search(body):
        reason = "il bot dichiara confidenza bassa"
    elif verdict != "APPROVATO":
        reason = f"verdetto {verdict}"
    else:
        approved = True
    return {"status": status, "request_id": request_id, "verdict": verdict, "approved": approved, "reason": reason}


def review(cfg: dict, cid: str, rationale: str) -> dict:
    state = load_state()
    att = _attempt(state, cid)
    chk = att.get("check") or {}
    head = git(att["worktree"], "rev-parse", "HEAD")
    if not chk.get("ok") or chk.get("sha") != head:
        raise AutofixError("Prima della revisione serve un `check` riuscito sul commit attuale.")
    if len(att["reviews"]) >= int(cfg["max_review_rounds"]):
        raise AutofixError(f"Raggiunto il massimo di {cfg['max_review_rounds']} revisioni: abbandona il tentativo.")
    profile = att.get("reviewer_profile")
    if not profile:
        raise AutofixError(f"Nessun reviewer_profile configurato per {att['repo']}.")

    rv = cfg["reviewer"]
    diff = git(att["worktree"], "diff", "-U8", f"{att['base_sha']}..HEAD")
    question = build_question(att, diff, rationale, int(rv["max_diff_chars"]))
    argv = [a.replace("{profile}", profile) for a in rv["command"]] + [question]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=int(rv["timeout"]))
        output = (r.stdout or "") + (("\n" + r.stderr) if r.returncode and r.stderr else "")
    except (OSError, subprocess.TimeoutExpired) as e:
        output = f"[error] {type(e).__name__}: {e}"
    result = parse_review(output)
    round_no = len(att["reviews"]) + 1

    out_dir = REPORTS_DIR / att["run_day"] / "autofix"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{cid.replace('@', '-')}-review-{round_no}.md"
    path.write_text(f"# Revisione {round_no} — {cid}\n\nCommit revisionato: `{head}`\n\n"
                    f"Esito: **{result['verdict'] or '—'}** · approvata: {result['approved']}"
                    f"{' · ' + result['reason'] if result['reason'] else ''}\n\n"
                    f"## Motivazione inviata\n\n{rationale}\n\n## Risposta del {rv['name']}\n\n{output.strip()}\n",
                    encoding="utf-8")
    att["reviews"].append({**result, "round": round_no, "sha": head, "at": _now(),
                           "rationale": rationale, "file": str(path)})
    att["status"] = "approved" if result["approved"] else "review_changes"
    _log(att, "review", sha=head, verdict=result["verdict"], approved=result["approved"])
    save_state(state)
    return {**result, "round": round_no, "file": str(path), "output": output}


# ══════════════════════════════════════════════════════════════
#  PUBLISH (unico varco verso l'esterno)
# ══════════════════════════════════════════════════════════════

def _remote_slug(url: str):
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _secret(pr_cfg: dict):
    """Token letto dall'ambiente o dal Portachiavi macOS. Non viene mai stampato né salvato."""
    env = pr_cfg.get("token_env")
    if env and os.environ.get(env):
        return os.environ[env]
    service = pr_cfg.get("keychain_service")
    if service and sys.platform == "darwin":
        argv = ["security", "find-generic-password", "-s", service, "-w"]
        if pr_cfg.get("keychain_account"):
            argv[3:3] = ["-a", pr_cfg["keychain_account"]]
        r = subprocess.run(argv, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def _pr_body(att: dict, cfg: dict) -> str:
    rv = next(r for r in reversed(att["reviews"]) if r["approved"])
    review_text = Path(rv["file"]).read_text(encoding="utf-8").split("## Risposta del", 1)[-1]
    review_text = review_text.split("\n", 1)[-1].strip()
    if "━━ BOZZA" in review_text:
        review_text = review_text.split("━━", 2)[-1].split("\n", 1)[-1].strip()
    findings = "\n".join(f"- **{f['severity']}** `{f['rule_id']}` `{f['file']}:{f['line']}` — {f['message']}"
                         for f in att["findings"])
    checks = "\n".join(f"- {'✅' if r['ok'] else '❌'} `{r['command']}`" for r in att["check"]["results"]) \
        or "- nessun check automatico configurato per questo repository"
    return (
        f"Correzione proposta in automatico dalla routine git-daily-review per i finding della review "
        f"del {att['day']} sul commit `{att['hash'][:9]}` ({att.get('author') or '?'}).\n\n"
        f"## Finding\n{findings}\n\n## Cosa cambia e perché\n{rv['rationale']}\n\n"
        f"## Controlli\n- diff: {len(att['check']['stats']['files'])} file, {att['check']['stats']['lines']} righe\n{checks}\n\n"
        f"## Revisione del {cfg['reviewer']['name']} (round {rv['round']}, verdetto {rv['verdict']})\n\n{review_text}\n\n"
        "---\nNessun merge automatico: questa PR va rivista e unita da una persona."
    )


def _open_pr_bitbucket(att, cfg, pr_cfg, title, body):
    ws, slug = pr_cfg.get("workspace"), pr_cfg.get("repo_slug")
    if not (ws and slug):
        ws, slug = _remote_slug(git(att["repo_path"], "remote", "get-url", "origin"))
    new_url = (f"https://bitbucket.org/{ws}/{slug}/pull-requests/new?source="
               f"{urllib.parse.quote(att['branch'], safe='')}&dest={urllib.parse.quote(att['base_branch'], safe='')}")
    token = _secret(pr_cfg)
    if not token:
        return None, f"PR da aprire a mano (nessun token Bitbucket): {new_url}"
    user = pr_cfg.get("user") or (os.environ.get(pr_cfg["user_env"]) if pr_cfg.get("user_env") else None)
    auth = ("Basic " + base64.b64encode(f"{user}:{token}".encode()).decode()) if user else f"Bearer {token}"
    payload = {"title": title, "description": body, "close_source_branch": True,
               "source": {"branch": {"name": att["branch"]}},
               "destination": {"branch": {"name": att["base_branch"]}}}
    req = urllib.request.Request(
        f"https://api.bitbucket.org/2.0/repositories/{ws}/{slug}/pullrequests",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": auth, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        return data.get("links", {}).get("html", {}).get("href"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        return None, f"Bitbucket ha risposto {e.code}: {detail} — apri la PR da {new_url}"
    except urllib.error.URLError as e:
        return None, f"Bitbucket non raggiungibile ({e.reason}): apri la PR da {new_url}"


def _open_pr_github(att, cfg, pr_cfg, title, body):
    argv = ["gh", "pr", "create", "--head", att["branch"], "--base", att["base_branch"],
            "--title", title, "--body", body]
    if pr_cfg.get("repo"):
        argv += ["--repo", pr_cfg["repo"]]
    r = subprocess.run(argv, cwd=att["worktree"], capture_output=True, text=True)
    if r.returncode != 0:
        return None, f"gh pr create fallito: {(r.stderr or r.stdout).strip()[:300]}"
    return r.stdout.strip().splitlines()[-1], None


def _published_on(state: dict, day: str) -> int:
    return sum(1 for a in state["attempts"].values()
               if a["status"] in ("published", "pushed") and (a.get("published_at") or "").startswith(day))


def publish_gates(cfg: dict, att: dict, state: dict, day: str) -> list:
    """Condizioni che devono valere tutte prima del push. Ritorna l'elenco dei blocchi (vuoto = via libera)."""
    repo_cfg = cfg["repos"].get(att["repo"]) or {}
    wt = att["worktree"]
    head = git(wt, "rev-parse", "HEAD")
    blocks = []
    if att["status"] in TERMINAL:
        blocks.append(f"tentativo già chiuso ({att['status']})")
    if not att["branch"].startswith(cfg["branch_prefix"]) or _is_protected(att["branch"], cfg, repo_cfg):
        blocks.append(f"branch {att['branch']} non pubblicabile")
    if git(wt, "rev-parse", "--abbrev-ref", "HEAD") != att["branch"]:
        blocks.append("il worktree non è sul branch autofix atteso")
    if git(wt, "status", "--porcelain"):
        blocks.append("modifiche non committate nel worktree")
    chk = att.get("check") or {}
    if not (chk.get("ok") and chk.get("sha") == head):
        blocks.append("manca un check riuscito sul commit in testa")
    last = att["reviews"][-1] if att["reviews"] else {}
    if not (last.get("approved") and last.get("sha") == head):
        blocks.append(f"manca l'approvazione del {cfg['reviewer']['name']} sul commit in testa")
    if subprocess.run(["git", "merge-base", "--is-ancestor", att["base_sha"], head], cwd=wt).returncode != 0:
        blocks.append("il branch non parte dal commit di base registrato")
    if _published_on(state, day) >= int(cfg["max_prs_per_day"]):
        blocks.append(f"raggiunto il massimo di {cfg['max_prs_per_day']} PR per oggi")
    if not (repo_cfg.get("pr") or {}).get("host"):
        blocks.append(f"nessun host PR configurato per {att['repo']}")
    return blocks


def publish(cfg: dict, cid: str, day: str, dry_run: bool = False) -> dict:
    state = load_state()
    att = _attempt(state, cid)
    blocks = publish_gates(cfg, att, state, day)
    pr_cfg = (cfg["repos"].get(att["repo"]) or {}).get("pr") or {}
    first = att["findings"][0]
    title = f"[autofix] {first['rule_id']}: {first['message']}"[:150]
    plan = {"id": cid, "push": f"git push origin {att['branch']}:refs/heads/{att['branch']}",
            "pr": f"{pr_cfg.get('host')}: {att['branch']} → {att['base_branch']}", "title": title, "blocks": blocks}
    if blocks or dry_run:
        return plan

    git(att["worktree"], "push", "origin", f"{att['branch']}:refs/heads/{att['branch']}", timeout=600)
    _log(att, "pushed", sha=git(att["worktree"], "rev-parse", "HEAD"))
    body = _pr_body(att, cfg)
    opener = {"bitbucket": _open_pr_bitbucket, "github": _open_pr_github}.get(pr_cfg["host"])
    url, problem = opener(att, cfg, pr_cfg, title, body) if opener else (None, "solo push (host senza PR)")
    att["status"] = "published" if url else "pushed"
    att["published_at"] = _now()
    att["pr_url"] = url
    att["pr_problem"] = problem
    if not url:
        # PR da aprire a mano: titolo e descrizione pronti da incollare
        draft = REPORTS_DIR / att["run_day"] / "autofix" / f"{cid.replace('@', '-')}-pr.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        att["pr_draft"] = str(draft)
    _log(att, att["status"], url=url, problem=problem)
    save_state(state)
    return {**plan, "status": att["status"], "pr_url": url, "problem": problem}


# ══════════════════════════════════════════════════════════════
#  CHIUSURE, REPORT, PULIZIA
# ══════════════════════════════════════════════════════════════

def close(cfg: dict, cid: str, day: str, status: str, reason: str) -> dict:
    state = load_state()
    att = state["attempts"].get(cid)
    if not att:
        c = _candidate(cfg, cid, day)
        att = {"id": cid, "repo": c["repo"], "hash": c["hash"], "day": c["day"], "run_day": day,
               "findings": c["findings"], "author": c["author"], "reviews": []}
        state["attempts"][cid] = att
    elif att.get("status") in TERMINAL:
        raise AutofixError(f"{cid} è già chiuso ({att['status']}).")
    att["status"] = status
    att["reason"] = reason
    att["closed_at"] = _now()
    _log(att, status, reason=reason)
    if att.get("worktree") and Path(att["worktree"]).is_dir():
        git(att["repo_path"], "worktree", "remove", "--force", att["worktree"], check=False)
    save_state(state)
    return att


def render_report(day: str, state: dict = None) -> str:
    state = state if state is not None else load_state()
    todays = [a for a in state["attempts"].values() if a.get("run_day") == day]
    icon = {"published": "✅", "pushed": "⬆️", "skipped": "⏭️", "abandoned": "🛑"}
    lines = [f"# Autofix — {day}", ""]
    if not todays:
        lines.append("Nessun finding trattato oggi.")
        return "\n".join(lines) + "\n"
    for a in todays:
        st = a.get("status", "?")
        f = a["findings"][0]
        lines.append(f"## {icon.get(st, '⏳')} {a['id']} — {st}")
        lines.append(f"- finding: **{f['severity']}** `{f['rule_id']}` `{f['file']}:{f['line']}` — {f['message']}"
                     + (f" (+{len(a['findings']) - 1})" if len(a["findings"]) > 1 else ""))
        if a.get("branch"):
            lines.append(f"- branch: `{a['branch']}` → `{a.get('base_branch')}`")
        for rv in a.get("reviews", []):
            lines.append(f"- revisione {rv['round']}: {rv['verdict'] or '—'}"
                         + (f" ({rv['reason']})" if rv.get("reason") else "") + f" · [{Path(rv['file']).name}](autofix/{Path(rv['file']).name})")
        if a.get("pr_url"):
            lines.append(f"- PR: {a['pr_url']}")
        if a.get("pr_problem"):
            lines.append(f"- ⚠️ {a['pr_problem']}")
        if a.get("pr_draft"):
            lines.append(f"- titolo e descrizione da incollare: [{Path(a['pr_draft']).name}](autofix/{Path(a['pr_draft']).name})")
        if a.get("reason"):
            lines.append(f"- motivo: {a['reason']}")
        if a.get("check") and a["check"].get("problems"):
            lines.append("- check: " + "; ".join(a["check"]["problems"]))
        lines.append("")
    return "\n".join(lines)


def write_report(day: str) -> Path:
    path = REPORTS_DIR / day / "autofix.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(day), encoding="utf-8")
    return path


def cleanup(cfg: dict, days: int) -> list:
    """Rimuove i worktree dei tentativi chiusi da più di `days` giorni (i branch restano)."""
    state = load_state()
    limit = (datetime.now() - timedelta(days=days)).isoformat()
    removed = []
    for a in state["attempts"].values():
        done_at = a.get("published_at") or a.get("closed_at") or ""
        if a.get("status") in TERMINAL and done_at and done_at < limit and a.get("worktree") \
                and Path(a["worktree"]).is_dir():
            git(a["repo_path"], "worktree", "remove", "--force", a["worktree"], check=False)
            removed.append(a["worktree"])
    return removed


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def _print(obj, as_json: bool = True):
    print(json.dumps(obj, ensure_ascii=False, indent=2) if as_json else obj)


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--date", default=date.today().isoformat(), help="giorno della run (YYYY-MM-DD)")
    p = argparse.ArgumentParser(description="Fix autonomi dei finding con revisione del bot e PR")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name):
        return sub.add_parser(name, parents=[common])

    c = cmd("candidates")
    c.add_argument("--json", action="store_true")
    for name in ("prepare", "check"):
        cmd(name).add_argument("id")
    r = cmd("review")
    r.add_argument("id")
    r.add_argument("--rationale", required=True, help="cosa cambia la correzione e perché")
    pb = cmd("publish")
    pb.add_argument("id")
    pb.add_argument("--dry-run", action="store_true")
    for name in ("skip", "abandon"):
        s = cmd(name)
        s.add_argument("id")
        s.add_argument("--reason", required=True)
    cmd("report")
    cmd("status")
    cl = cmd("cleanup")
    cl.add_argument("--days", type=int, default=14)
    a = p.parse_args(argv)

    try:
        if a.cmd == "report":
            print(write_report(a.date))
            return 0
        if a.cmd == "status":
            _print({k: {x: v.get(x) for x in ("status", "branch", "pr_url", "reason")}
                    for k, v in load_state()["attempts"].items()})
            return 0
        cfg = load_config()
        if not cfg.get("enabled", True):
            print("autofix disattivato in routine/autofix.yaml")
            return 0
        if a.cmd == "candidates":
            cands = find_candidates(cfg, a.date)
            if a.json:
                _print(cands)
            else:
                for x in cands:
                    print(f"{x['id']}  [{x['status']}]  {x['branch']}  {len(x['findings'])} finding")
                    for f in x["findings"]:
                        print(f"    {f['severity']:8} {f['rule_id']:10} {f['file']}:{f['line']} — {f['message']}")
                if not cands:
                    print("Nessun candidato.")
            return 0
        if a.cmd == "prepare":
            att = prepare(cfg, a.id, a.date)
            _print({k: att[k] for k in ("id", "worktree", "branch", "base_branch", "base_sha", "commit_in_base", "findings")})
            return 0
        if a.cmd == "check":
            res = check(cfg, a.id)
            _print(res)
            return 0 if res["ok"] else 2
        if a.cmd == "review":
            res = review(cfg, a.id, a.rationale)
            print(res.pop("output"))
            _print(res)
            return 0 if res["approved"] else 2
        if a.cmd == "publish":
            res = publish(cfg, a.id, a.date, dry_run=a.dry_run)
            _print(res)
            return 2 if res["blocks"] else 0
        if a.cmd in ("skip", "abandon"):
            att = close(cfg, a.id, a.date, "skipped" if a.cmd == "skip" else "abandoned", a.reason)
            _print({"id": att["id"], "status": att["status"], "reason": att["reason"]})
            return 0
        if a.cmd == "cleanup":
            _print(cleanup(cfg, a.days))
            return 0
    except AutofixError as e:
        print(f"✋ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
