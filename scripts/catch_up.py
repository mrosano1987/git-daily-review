"""
catch_up.py — Recupero delle run saltate

A ogni run pianificata, prima della review di oggi, si controllano i giorni
lavorativi (lun–ven) recenti. Una run è "saltata" quando per quel giorno:

- non esiste alcun report (app chiusa, macchina spenta, …);
- il report c'è ma il `git fetch` è fallito (dati incompleti);
- la run è partita prima dell'orario previsto (es. alle 10:24 invece che
  alle 17:00): ha visto solo i commit del mattino.

Un giorno è coperto se una run valida (fetch riuscito, eseguita dopo
l'orario previsto) ha raccolto l'intero giorno, sia essa la run normale di
quel giorno o una run di recupero che lo includeva.

Le run saltate si recuperano con UNA sola run sull'intervallo continuo che
va dall'inizio della più vecchia da recuperare fino a oggi alle 00:00,
escludendo i commit già revisionati (dalla base dati). Il report va nella
cartella dell'ultimo giorno saltato e registra l'intervallo coperto.

Al massimo 6 run vengono recuperate senza chiedere (le più recenti). Oltre:
da terminale si chiede conferma; nelle run automatiche le più vecchie
restano "in attesa di conferma" e vengono segnalate nel report di oggi, con
i comandi per recuperarle (`--catch-up-all`) o ignorarle
(`--catch-up-ignore-before`).
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT_DIR / "data" / "catch_up.json"

MAX_AUTO = 6              # run recuperate senza chiedere
LOOKBACK_DAYS = 30        # finestra del controllo (giorni di calendario)
WORKDAYS = {0, 1, 2, 3, 4}

_GENERATED = re.compile(r"Generato il (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass
class Skipped:
    day: str
    reason: str


@dataclass
class Plan:
    skipped: list = field(default_factory=list)    # tutte le run saltate nella finestra
    recover: list = field(default_factory=list)    # da recuperare ora (vecchia → recente)
    pending: list = field(default_factory=list)    # in attesa di conferma (più vecchie)
    since: str = ""
    until: str = ""
    folder: str = ""                               # cartella del report di recupero

    @property
    def needed(self) -> bool:
        return bool(self.recover)


# ══════════════════════════════════════════════════════════════
#  STATO (decisioni dell'utente)
# ══════════════════════════════════════════════════════════════

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def ignore_before(day: str):
    """Le run saltate prima di questa data non vengono più segnalate."""
    datetime.strptime(day, "%Y-%m-%d")
    state = load_state()
    state["ignore_before"] = day
    if state.get("confirm_before", "") <= day:
        state.pop("confirm_before", None)
    save_state(state)


def mark_done(p: "Plan"):
    """
    Dopo un recupero: le run lasciate in attesa restano in attesa (non
    vengono recuperate a blocchi di 6 nei giorni successivi) finché l'utente
    non le recupera con --catch-up-all o le ignora.
    """
    state = load_state()
    if p.pending:
        state["confirm_before"] = p.recover[0].day
    else:
        state.pop("confirm_before", None)
    save_state(state)


# ══════════════════════════════════════════════════════════════
#  RILEVAMENTO
# ══════════════════════════════════════════════════════════════

def _dt(s: str):
    try:
        return datetime.fromisoformat(s[:19])
    except (TypeError, ValueError):
        return None


def _run_info(day_dir: Path):
    """Esito di una cartella report: intervallo coperto, ora della raccolta, validità."""
    jsons = sorted(day_dir.glob("*-commits.json"))
    if not jsons:
        return None
    day = day_dir.name
    start = datetime.strptime(day, "%Y-%m-%d")
    since, until, collected, fetch_ok = start, start + timedelta(days=1), None, True
    for jp in jsons:
        try:
            data = json.loads(jp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("fetch_ok") is False or data.get("errors"):
            fetch_ok = False
        cov = data.get("coverage") or {}
        since = min(since, _dt(cov.get("since")) or since)
        until = max(until, _dt(cov.get("until")) or until)
        c = _dt(data.get("collected_at"))
        collected = c if c and (collected is None or c < collected) else collected

    # Report precedenti al campo collected_at: ora di generazione del report.
    if collected is None:
        model = day_dir / "dashboard-data.json"
        try:
            collected = _dt(json.loads(model.read_text(encoding="utf-8")).get("generated_at"))
        except (OSError, json.JSONDecodeError):
            collected = None
    if collected is None:
        summary = day_dir / "daily-summary.md"
        try:
            m = _GENERATED.search(summary.read_text(encoding="utf-8", errors="replace")[:600])
            collected = _dt(m.group(1)) if m else None
        except OSError:
            pass
    if collected is None:
        collected = datetime.fromtimestamp(min(p.stat().st_mtime for p in jsons))
    return {"day": day, "since": since, "until": until, "collected": collected, "fetch_ok": fetch_ok}


def find_skipped(reports_dir: Path, today: str, run_hour: int,
                 lookback_days: int = LOOKBACK_DAYS, ignore_before_day: str = None) -> list:
    """Run saltate nei giorni lavorativi della finestra, esclusa oggi (vecchia → recente)."""
    reports_dir = Path(reports_dir)
    runs = [r for r in (_run_info(d) for d in reports_dir.iterdir()
                        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)) if r] \
        if reports_dir.is_dir() else []
    by_day = {r["day"]: r for r in runs}

    t = datetime.strptime(today, "%Y-%m-%d")
    first = t - timedelta(days=lookback_days)
    if ignore_before_day:
        first = max(first, datetime.strptime(ignore_before_day, "%Y-%m-%d"))

    skipped = []
    d = first
    while d < t:
        if d.weekday() in WORKDAYS:
            day_end = d + timedelta(days=1)
            due = d.replace(hour=run_hour)       # la run del giorno conta da quest'ora
            covered = any(r["fetch_ok"] and r["since"] <= d and r["until"] >= day_end
                          and r["collected"] >= min(due, r["until"]) for r in runs)
            if not covered:
                own = by_day.get(d.strftime("%Y-%m-%d"))
                if own is None:
                    reason = "nessuna run"
                elif not own["fetch_ok"]:
                    reason = "git fetch fallito"
                else:
                    reason = f"run anticipata (alle {own['collected']:%H:%M})"
                skipped.append(Skipped(d.strftime("%Y-%m-%d"), reason))
        d += timedelta(days=1)
    return skipped


def plan(reports_dir: Path, today: str, run_hour: int, recover_all: bool = False,
         lookback_days: int = LOOKBACK_DAYS) -> Plan:
    state = load_state()
    skipped = find_skipped(reports_dir, today, run_hour, lookback_days, state.get("ignore_before"))
    p = Plan(skipped=skipped)
    if not skipped:
        return p
    if recover_all:
        p.recover = skipped
    else:
        # Le run già segnalate come "in attesa di conferma" non si recuperano da sole.
        boundary = state.get("confirm_before", "")
        waiting = [s for s in skipped if s.day < boundary]
        candidates = [s for s in skipped if s.day >= boundary]
        p.recover = candidates[-MAX_AUTO:]
        p.pending = waiting + candidates[:-MAX_AUTO]
    if not p.recover:
        return p
    p.since = f"{p.recover[0].day}T00:00:00"
    p.until = f"{today}T00:00:00"
    p.folder = p.recover[-1].day
    return p


def include_pending(p: Plan) -> Plan:
    """Conferma ricevuta: recupera anche le run in attesa."""
    if p.pending:
        p.recover = p.pending + p.recover
        p.pending = []
        p.since = f"{p.recover[0].day}T00:00:00"
    return p


# ══════════════════════════════════════════════════════════════
#  TESTI PER I REPORT
# ══════════════════════════════════════════════════════════════

def _fmt(days: list) -> str:
    return ", ".join(f"{s.day} ({s.reason})" for s in days)


def catch_up_meta(p: Plan) -> dict:
    """Metadati salvati nel JSON della run di recupero."""
    return {"recovered": [{"day": s.day, "reason": s.reason} for s in p.recover],
            "pending": [{"day": s.day, "reason": s.reason} for s in p.pending],
            "since": p.since, "until": p.until}


def recovery_notice(p: Plan, reviewed_excluded: int) -> str:
    # reviewed_excluded: commit dell'intervallo già revisionati e quindi esclusi
    """Intestazione Markdown del report di recupero."""
    lines = [
        "## 🔁 Run di recupero",
        "",
        f"Questa run sostituisce **{len(p.recover)} run saltate**: {_fmt(p.recover)}.",
        "",
        f"Intervallo coperto: **{p.since.replace('T', ' ')} → {p.until.replace('T', ' ')}** "
        f"(i commit già revisionati sono esclusi: {reviewed_excluded}).",
    ]
    if p.pending:
        lines += ["", pending_text(p)]
    return "\n".join(lines) + "\n"


def pending_text(p: Plan) -> str:
    oldest = p.pending[0].day
    return (f"⚠️ **{len(p.pending)} run saltate più vecchie in attesa di conferma** "
            f"(oltre il limite di {MAX_AUTO}): {_fmt(p.pending)}. "
            f"Per recuperarle: `python3 scripts/daily_review.py --catch-up-only --catch-up-all`; "
            f"per non segnalarle più: `python3 scripts/daily_review.py --catch-up-ignore-before "
            f"{(datetime.strptime(p.pending[-1].day, '%Y-%m-%d') + timedelta(days=1)):%Y-%m-%d}` "
            f"(la più vecchia è del {oldest}).")


def today_notice(p: Plan, outcome: str) -> str:
    """Nota per il report di oggi: cosa ha fatto il controllo preventivo."""
    if not p.skipped:
        return ""
    lines = ["## 🔁 Controllo run saltate", ""]
    if outcome == "none":
        lines.append("Nessuna nuova run saltata da recuperare.")
    if outcome == "done":
        lines.append(f"Recuperate **{len(p.recover)} run saltate** in un'unica run "
                     f"({_fmt(p.recover)}): report in `reports/{p.folder}/`.")
    elif outcome == "postponed":
        lines.append(f"⚠️ **Recupero rimandato**: `git fetch` non riuscito, i dati sarebbero "
                     f"incompleti. Run ancora da recuperare: {_fmt(p.recover)}.")
    elif outcome == "failed":
        lines.append(f"⚠️ **Recupero fallito** (vedi log). Run ancora da recuperare: {_fmt(p.recover)}.")
    if p.pending:
        lines += ["", pending_text(p)]
    return "\n".join(lines) + "\n"


def describe(p: Plan) -> str:
    """Riepilogo testuale per il terminale (--catch-up-check)."""
    if not p.skipped:
        return "Nessuna run saltata nella finestra di controllo."
    out = [f"Run saltate: {len(p.skipped)}"]
    out += [f"  • {s.day} — {s.reason}" + ("   [in attesa di conferma]" if s in p.pending else "")
            for s in p.skipped]
    if p.recover:
        out.append(f"Recupero: {len(p.recover)} run in un'unica run, intervallo "
                   f"{p.since} → {p.until}, report in reports/{p.folder}/")
    return "\n".join(out)
