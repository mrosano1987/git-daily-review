"""
notify.py — Notifiche sull'esito delle run giornaliere (Teams o Telegram)

Un messaggio quando una run si chiude, uno quando la riserva subentra a una
run interrotta e, dal watchdog serale, uno quando la giornata non risulta
chiusa da nessun harness.

I messaggi contengono SOLO esiti e conteggi (stato, harness, orari, numero
di commit, quality score, finding per severità, esiti dell'autofix, avvisi da
un elenco chiuso): nessun nome di repository, file, autore o testo di
finding.

Configurazione in routine/agent.yaml (un solo canale; `channel` sceglie se
ce ne sono due):

    notify:
      label: git-daily-review
      teams:                           # Workflow "Post to a channel when a webhook request is received"
        keychain_service: git-daily-review-teams   # URL del Workflow nel Portachiavi
        webhook_env: GDR_TEAMS_WEBHOOK             # alternativa al Portachiavi
        auth: azure-cli                # se il trigger accetta solo utenti del tenant
      telegram:
        chat_id: "-1001234567890"      # o "@nome_canale"
        keychain_service: git-daily-review-telegram
        token_env: GDR_TELEGRAM_TOKEN

L'URL del Workflow si tratta come un token, mai stampato né scritto in chiaro.
Con il trigger aperto a «Chiunque» l'URL contiene la firma sig=; con «Qualsiasi
utente nel tenant» serve `auth: azure-cli`: a ogni invio si chiede ad Azure CLI
(dopo un `az login`) un token del proprio account per Power Automate, senza
salvarlo.

Uso:
    python3 scripts/notify.py test                 # messaggio di prova
    python3 scripts/notify.py send [--date D]      # riepilogo del giorno
    python3 scripts/notify.py watchdog [--date D]  # avvisa se il giorno non è chiuso
    python3 scripts/notify.py chat-ids             # (Telegram) chat viste dal bot
    python3 scripts/notify.py flush                # reinvia i messaggi rimasti in coda

Se il canale non è raggiungibile il messaggio resta in coda in
data/notify-outbox.jsonl e parte al primo invio riuscito successivo (o al
watchdog serale) con l'orario originale; lo stesso finché la configurazione
è incompleta. Il job launchd routine/com.gitdailyreview.notify-flush.plist
ritenta ogni 30 minuti (a coda vuota non fa chiamate di rete).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _bootstrap
    _bootstrap.ensure_deps()

import yaml  # noqa: E402

import agent_run  # noqa: E402
from keychain import read_token  # noqa: E402

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("GDR_AGENT_CONFIG") or ROOT_DIR / "routine" / "agent.yaml")
REPORTS_DIR = Path(os.environ.get("GDR_REPORTS_DIR") or ROOT_DIR / "reports")
AUTOFIX_STATE = Path(os.environ.get("GDR_AUTOFIX_STATE") or ROOT_DIR / "data" / "autofix.json")
OUTBOX = Path(os.environ.get("GDR_NOTIFY_OUTBOX") or ROOT_DIR / "data" / "notify-outbox.jsonl")
MAX_QUEUED = 20          # oltre, si tengono i più recenti

WARNINGS = {
    "rag": "RAG non disponibile",
    "ingest": "ingest Confluence non eseguito",
    "ollama": "Ollama non raggiungibile: review saltata",
    "review": "review non completata",
    "autofix": "autofix non completato",
}
AUTOFIX_LABELS = (("published", "PR aperte"), ("pushed", "branch pubblicati (PR da aprire)"),
                  ("skipped", "saltati"), ("abandoned", "abbandonati"))


# ══════════════════════════════════════════════════════════════
#  CONFIGURAZIONE E INVIO
# ══════════════════════════════════════════════════════════════

def load_config() -> dict:
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return data.get("notify") or {}


class NotifyError(Exception):
    """Errore di invio già descritto in italiano, senza URL né token."""


CHANNELS = ("teams", "telegram")


def _channel(cfg: dict) -> tuple:
    """(nome, configurazione) del canale: notify.channel o il primo configurato."""
    name = cfg.get("channel") or next((c for c in CHANNELS if cfg.get(c)), None)
    if name not in CHANNELS or not cfg.get(name):
        return None, {}
    return name, cfg[name]


# ── Telegram ───────────────────────────────────────────────────

def _token(tg: dict) -> str:
    token = read_token(tg.get("token_env"), tg.get("keychain_service"), tg.get("keychain_account"))
    if not token:
        raise NotifyError("token del bot Telegram non disponibile")
    return token


def _call(token: str, method: str, params: dict = None) -> dict:
    """Chiamata alla Bot API. Gli errori non riportano mai l'URL, che contiene il token."""
    data = urllib.parse.urlencode(params).encode() if params else None
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data,
                                 method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("description", "")
        except ValueError:
            detail = ""
        raise NotifyError(f"Telegram ha risposto {e.code} {detail}".strip()) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NotifyError(f"Telegram non raggiungibile ({type(getattr(e, 'reason', e)).__name__})") from None
    if not body.get("ok"):
        raise NotifyError(f"Telegram ha rifiutato la richiesta ({body.get('description', '?')})")
    return body


# ── Teams (Workflows) ──────────────────────────────────────────

FLOW_RESOURCE = "https://service.flow.microsoft.com/"
AZ_CANDIDATES = ("/opt/homebrew/bin/az", "/usr/local/bin/az")


def _azure_token(tm: dict) -> str:
    """Token Entra ID dell'utente per Power Automate, da Azure CLI. Mai stampato né salvato."""
    az = tm.get("az_path") or shutil.which("az") or next((p for p in AZ_CANDIDATES if Path(p).exists()), None)
    if not az:
        raise NotifyError("Azure CLI (az) non trovato: brew install azure-cli")
    try:
        r = subprocess.run([az, "account", "get-access-token", "--resource", tm.get("resource", FLOW_RESOURCE),
                            "--query", "accessToken", "-o", "tsv"], capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise NotifyError(f"Azure CLI non risponde ({type(e).__name__})") from None
    if r.returncode != 0 or not r.stdout.strip():
        hint = "accesso scaduto o mancante: esegui az login" if "login" in r.stderr.lower() else "errore di az"
        raise NotifyError(f"token Azure non disponibile ({hint})")
    return r.stdout.strip()


def _teams_url(tm: dict, signed: bool = True) -> str:
    url = read_token(tm.get("webhook_env"), tm.get("keychain_service"), tm.get("keychain_account"))
    if not url:
        raise NotifyError("indirizzo del Workflow Teams non disponibile")
    if not url.startswith("https://"):
        raise NotifyError("indirizzo del Workflow Teams non valido (deve iniziare con https://)")
    if signed and "sig=" not in url:
        # due cause note: il prompt di `security … -w` tronca a 128 caratteri, oppure il trigger
        # del Workflow accetta solo utenti del tenant (OAuth) e l'URL nasce senza firma
        raise NotifyError("indirizzo del Workflow Teams senza firma sig=: salvalo con -w \"$(pbpaste)\" "
                          "e, nel trigger, imposta «Chi può attivare il flusso» su «Chiunque» "
                          "oppure usa auth: azure-cli")
    return url


def teams_card(text: str) -> dict:
    """Adaptive Card con una riga di testo per riga del messaggio, la prima in grassetto."""
    lines = text.splitlines() or [""]
    body = [{"type": "TextBlock", "text": lines[0], "weight": "Bolder", "wrap": True}]
    body += [{"type": "TextBlock", "text": line, "wrap": True, "spacing": "None"} for line in lines[1:]]
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard", "version": "1.4", "body": body}}]}


def _post_teams(url: str, text: str, token: str = None):
    """POST della card al Workflow. Gli errori non riportano mai l'URL né il token."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(teams_card(text)).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30):
            pass
    except urllib.error.HTTPError as e:
        try:
            code = (json.loads(e.read().decode()).get("error") or {}).get("code", "")
        except (ValueError, AttributeError):
            code = ""
        raise NotifyError(f"Teams ha risposto {e.code} {code}".strip()) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NotifyError(f"Teams non raggiungibile ({type(getattr(e, 'reason', e)).__name__})") from None


# ── invio con coda ─────────────────────────────────────────────

def _sender(name: str, conf: dict):
    """Funzione che invia un testo sul canale; solleva NotifyError se la configurazione è incompleta."""
    if name == "teams":
        if conf.get("auth") == "azure-cli":
            url, token = _teams_url(conf, signed=False), _azure_token(conf)
            return lambda text: _post_teams(url, text, token)
        url = _teams_url(conf)
        return lambda text: _post_teams(url, text)
    if not conf.get("chat_id"):       # canale non ancora configurato: i messaggi aspettano in coda
        raise NotifyError("chat_id del canale non ancora configurato")
    token = _token(conf)
    return lambda text: _call(token, "sendMessage", {"chat_id": conf["chat_id"], "text": text,
                                                     "disable_web_page_preview": "true"})


def _queue(items: list):
    OUTBOX.parent.mkdir(parents=True, exist_ok=True)
    OUTBOX.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items[-MAX_QUEUED:]),
                      encoding="utf-8")


def _queued() -> list:
    try:
        return [json.loads(line) for line in OUTBOX.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def send(text: str, cfg: dict = None) -> tuple:
    """Invia prima i messaggi in coda, poi `text`. Se il canale non risponde, ciò che resta va in coda.

    Ritorna (inviato, dettaglio); non solleva eccezioni.
    """
    cfg = cfg if cfg is not None else load_config()
    name, conf = _channel(cfg)
    if not name:
        return False, "notifiche non configurate (notify.teams o notify.telegram)"
    late = _queued()
    pending = late + ([{"at": datetime.now().isoformat(timespec="minutes"), "text": text}] if text else [])
    if not pending:
        return True, "nessun messaggio da inviare"
    try:
        deliver = _sender(name, conf)
        for i, item in enumerate(pending):
            body = item["text"]
            if i < len(late):
                body += f"\n(in ritardo: generato alle {item['at'][11:16]} del {item['at'][:10]})"
            try:
                deliver(body)
            except NotifyError:
                pending = pending[i:]
                raise
    except NotifyError as e:
        _queue(pending)
        return False, f"non inviata, in coda ({e})"
    OUTBOX.unlink(missing_ok=True)
    return True, f"inviata su {name}" + (f" (+{len(late)} dalla coda)" if late else "")


def chat_ids(cfg: dict = None) -> list:
    """Chat in cui il bot ha ricevuto aggiornamenti: serve a trovare il chat_id del canale."""
    cfg = cfg if cfg is not None else load_config()
    updates = _call(_token(cfg.get("telegram") or {}), "getUpdates").get("result", [])
    seen = {}
    for u in updates:
        for key in ("channel_post", "message", "my_chat_member", "edited_channel_post"):
            chat = (u.get(key) or {}).get("chat")
            if chat:
                seen[chat["id"]] = {"chat_id": chat["id"], "type": chat.get("type"),
                                    "title": chat.get("title") or chat.get("username")}
    return list(seen.values())


# ══════════════════════════════════════════════════════════════
#  TESTI (solo esiti e conteggi)
# ══════════════════════════════════════════════════════════════

def _hm(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "?"


def _review_line(day: str) -> str:
    try:
        d = json.loads((REPORTS_DIR / day / "dashboard-data.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "Review: nessun report per oggi (nessun commit o review non eseguita)"
    sev = d.get("findings_by_severity") or {}
    quality = d.get("quality_avg")
    return (f"Review: {d.get('totals', {}).get('commits', 0)} commit · qualità "
            f"{f'{quality:.1f}/10' if isinstance(quality, (int, float)) else 'n/d'} · finding "
            + ", ".join(f"{sev.get(k, 0)} {k}" for k in ("critical", "error", "warning")))


def _autofix_line(day: str) -> str:
    try:
        attempts = json.loads(AUTOFIX_STATE.read_text(encoding="utf-8")).get("attempts", {}).values()
    except (OSError, ValueError):
        attempts = []
    counts = Counter(a.get("status") for a in attempts if a.get("run_day") == day)
    if not counts:
        return "Autofix: nessun candidato trattato"
    parts = [f"{counts.pop(k)} {label}" for k, label in AUTOFIX_LABELS if counts.get(k)]
    if counts:
        parts.append(f"{sum(counts.values())} in sospeso")
    return "Autofix: " + ", ".join(parts)


def run_summary(day: str, cfg: dict = None) -> str:
    cfg = cfg if cfg is not None else load_config()
    label = cfg.get("label", "git-daily-review")
    runs = agent_run.load(day)
    done = runs.get("done") or {}
    status = done.get("status", "?")
    icon = {"ok": "✅", "partial": "⚠️"}.get(status, "❔")
    last = next((a for a in reversed(runs.get("attempts", [])) if a.get("harness") == done.get("harness")), {})
    lines = [f"{icon} {label} · {day} · {done.get('harness', '?')}",
             f"Stato: {status} · {_hm(last.get('started_at'))} → {_hm(done.get('at'))}",
             _review_line(day), _autofix_line(day)]
    lines += [f"⚠️ {WARNINGS[w]}" for w in done.get("warnings", []) if w in WARNINGS]
    interrupted = [a for a in runs.get("attempts", []) if str(a.get("outcome", "")).startswith("interrotta")]
    lines += [f"🔁 run {a['harness']} delle {_hm(a.get('started_at'))} interrotta" for a in interrupted]
    return "\n".join(lines)


def takeover_text(day: str, previous: dict, harness: str, cfg: dict = None) -> str:
    label = (cfg if cfg is not None else load_config()).get("label", "git-daily-review")
    return (f"🔁 {label} · {day}\nLa run su {previous.get('harness', '?')} avviata alle "
            f"{_hm(previous.get('started_at'))} non si è chiusa: subentra {harness}.")


def watchdog_text(day: str, cfg: dict = None) -> str:
    """Testo d'allarme se il giorno non è chiuso, altrimenti stringa vuota."""
    label = (cfg if cfg is not None else load_config()).get("label", "git-daily-review")
    runs = agent_run.load(day)
    if runs.get("done"):
        return ""
    lines = [f"🚨 {label} · {day}: nessuna run completata"]
    for a in runs.get("attempts", []):
        lines.append(f"- {a.get('harness')} avviata alle {_hm(a.get('started_at'))}: {a.get('outcome', '?')}")
    if runs.get("running"):
        r = runs["running"]
        lines.append(f"- {r['harness']} avviata alle {_hm(r['started_at'])}: ancora aperta (interrotta o in corso)")
    if len(lines) == 1:
        lines.append("Nessuna run è partita: app chiuse, Mac spento o task disattivati?")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--date", default=date.today().isoformat())
    p = argparse.ArgumentParser(description="Notifiche (Teams o Telegram) delle run giornaliere")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test", parents=[common])
    sub.add_parser("send", parents=[common])
    w = sub.add_parser("watchdog", parents=[common])
    w.add_argument("--always", action="store_true", help="invia il riepilogo anche se il giorno è chiuso")
    sub.add_parser("chat-ids", parents=[common])
    sub.add_parser("flush", parents=[common])
    a = p.parse_args(argv)

    if a.cmd == "chat-ids":
        try:
            found = chat_ids()
        except NotifyError as e:
            print(f"✋ {e}", file=sys.stderr)
            return 1
        for c in found:
            print(json.dumps(c, ensure_ascii=False))
        if not found:
            print("Nessuna chat: aggiungi il bot al canale come amministratore e pubblica un messaggio, poi riprova.")
        return 0
    if a.cmd == "flush":
        ok, detail = send("")
        print(f"coda: {detail}")
        return 0 if ok else 1
    if a.cmd == "test":
        text = f"🔔 {load_config().get('label', 'git-daily-review')}: notifiche attive ({datetime.now():%Y-%m-%d %H:%M})"
    elif a.cmd == "send":
        text = run_summary(a.date)
    else:
        text = watchdog_text(a.date) or (run_summary(a.date) if a.always else "")
        if not text:
            ok, detail = send("")          # giornata chiusa: si limita a svuotare la coda
            print(f"giornata {a.date} chiusa: nessun avviso · coda: {detail}")
            return 0
    ok, detail = send(text)
    print(f"notifica {detail}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
