"""
agent_run.py — Una sola run dell'agente giornaliero al giorno, qualunque harness la esegua

L'agente può girare su più harness (Claude Code, Antigravity, …), ciascuno
con il proprio scheduler: uno principale e uno di riserva più tardi, per
quando il principale non parte o si ferma (app chiusa, credito esaurito).
Questo file è il punto d'incontro:

    begin --harness X   → 0: procedi · 3: giornata già completata · 4: run in corso
    end   --harness X   → registra la fine (ok | partial) e libera il lock
    status              → stato del giorno

Una run "in corso" da più di `--stale-hours` ore si considera interrotta
(sessione chiusa, credito finito a metà) e la riserva può subentrare.
Stato in data/agent-runs/<giorno>.json (gitignored).
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = Path(os.environ.get("GDR_AGENT_RUNS_DIR") or ROOT_DIR / "data" / "agent-runs")

PROCEED, DONE, RUNNING = 0, 3, 4


def _path(day: str) -> Path:
    return RUNS_DIR / f"{day}.json"


def load(day: str) -> dict:
    try:
        return json.loads(_path(day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"day": day, "attempts": []}


def _save(day: str, data: dict):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(day).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_path(day))


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def begin(day: str, harness: str, stale_hours: float = 4, now: datetime = None) -> tuple:
    now = now or _now()
    data = load(day)
    if data.get("done"):
        d = data["done"]
        return DONE, f"giornata {day} già completata da {d['harness']} alle {d['at']} ({d['status']})"
    running = data.get("running")
    if running and now - datetime.fromisoformat(running["started_at"]) < timedelta(hours=stale_hours):
        return RUNNING, f"run in corso su {running['harness']} dalle {running['started_at']}"
    if running:
        running["outcome"] = f"interrotta (subentra {harness})"
        data["attempts"].append(running)
    data["running"] = {"harness": harness, "started_at": now.isoformat()}
    _save(day, data)
    return PROCEED, f"run avviata su {harness}" + (f" (la precedente su {running['harness']} era interrotta)" if running else "")


def end(day: str, harness: str, status: str = "ok", note: str = "", now: datetime = None) -> str:
    now = now or _now()
    data = load(day)
    running = data.pop("running", None) or {"harness": harness, "started_at": None}
    running.update(outcome=status, ended_at=now.isoformat())
    data["attempts"].append(running)
    data["done"] = {"harness": harness, "at": now.isoformat(), "status": status, "note": note}
    _save(day, data)
    return f"giornata {day} chiusa da {harness} ({status})"


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--date", default=date.today().isoformat())
    p = argparse.ArgumentParser(description="Lock e marker della run giornaliera dell'agente")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("begin", parents=[common])
    b.add_argument("--harness", required=True)
    b.add_argument("--stale-hours", type=float, default=4)
    e = sub.add_parser("end", parents=[common])
    e.add_argument("--harness", required=True)
    e.add_argument("--status", choices=["ok", "partial"], default="ok")
    e.add_argument("--note", default="")
    sub.add_parser("status", parents=[common])
    a = p.parse_args(argv)

    if a.cmd == "begin":
        code, msg = begin(a.date, a.harness, a.stale_hours)
        print(msg)
        return code
    if a.cmd == "end":
        print(end(a.date, a.harness, a.status, a.note))
        return 0
    print(json.dumps(load(a.date), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
