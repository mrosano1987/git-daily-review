# Setup — Skill + Routine giornaliera

## Perché in locale e non come Routine cloud di Claude

Le Routine cloud girano come sessioni remote "fresche": non vedono la tua
macchina, quindi né i cloni locali dei repo né Ollama. Questo tool DEVE
girare in locale. Lo schema qui sotto ti dà lo stesso risultato (review +
digest intelligente ogni giorno alle 17:00) interamente sulla tua macchina.

Architettura della routine:

    17:00  launchd/cron → daily-routine.sh
           ├── FASE 1  python3 scripts/daily_review.py   (motore, Ollama, gratis)
           └── FASE 2  claude -p + skill daily-review    (digest, sola lettura)
                        → reports/<oggi>/digest.md

## 1. Installare la skill (una volta)

La skill è di PROGETTO (in `.claude/skills/` dentro il repo): committala e
tutto il team la eredita automaticamente clonando il repo.

```bash
# dalla root di git-daily-review:
cp -r <questo-pacchetto>/.claude .
git add .claude && git commit -m "Add daily-review project skill" && git push
```

Verifica: apri `claude` nella cartella del progetto e digita `/daily-review`
(o semplicemente chiedi "esegui la review di oggi" — la skill si attiva da
sola quando serve).

## 2. Installare la routine giornaliera

```bash
# dalla root di git-daily-review:
mkdir -p routine && cp <questo-pacchetto>/routine/daily-routine.sh routine/
chmod +x routine/daily-routine.sh

# Prova manuale PRIMA di schedulare (importante):
GDR_PROJECT_DIR="$(pwd)" ./routine/daily-routine.sh
cat "reports/$(date +%F)/digest.md"
```

### macOS (launchd) — consigliato sul tuo Mac

Crea `~/Library/LaunchAgents/com.gitdailyreview.routine.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.gitdailyreview.routine</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>GDR_PROJECT_DIR=$HOME/Progetti/git-daily-review $HOME/Progetti/git-daily-review/routine/daily-routine.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>17</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>/tmp/gdr-routine.out</string>
    <key>StandardErrorPath</key><string>/tmp/gdr-routine.err</string>
</dict>
</plist>
```

(adatta i path se il progetto non è in ~/Progetti). Poi:

```bash
launchctl load ~/Library/LaunchAgents/com.gitdailyreview.routine.plist
launchctl start com.gitdailyreview.routine   # test immediato
```

Nota: `bash -lc` carica il tuo profilo di shell, così `claude`, `python3`
e `ollama` sono nel PATH anche quando lancia launchd.

### Linux (cron)

```bash
crontab -e
# aggiungi:
0 17 * * 1-5 GDR_PROJECT_DIR=$HOME/Progetti/git-daily-review bash -lc '$HOME/Progetti/git-daily-review/routine/daily-routine.sh'
```

(`1-5` = solo giorni lavorativi; togli se vuoi anche il weekend)

## 3. Cosa ottieni ogni giorno alle 17

- `reports/<data>/daily-summary.md` — il report tecnico completo (motore)
- `reports/<data>/digest.md` — il digest da team lead: headline con trend,
  critici con gate violati, segnali di mentoring per autore, KB suggestion
  pending con raccomandazione, un'azione consigliata per domani

E la mattina dopo puoi aprire Claude Code e lavorarci sopra in modo
conversazionale: "approva le suggestion che raccomandavi ieri", "confronta
il trend dell'ultima settimana", ecc. — la skill sa già come fare.

## Guardrail inclusi (perché fidarsi della run autonoma)

- Il digest gira con **allow-list di soli strumenti di lettura** + Write
  limitato a `reports/` + `--permission-mode dontAsk` (tutto il resto viene
  auto-negato) e `--max-turns 25` come circuit breaker
- La skill vieta a Claude di modificare la knowledge base direttamente e
  di approvare autonomamente suggestion critiche
- Se il motore fallisce o non ci sono commit, il digest non parte (zero
  token spesi a vuoto)
- Ogni run è loggata in `logs/routine.log`

## Nota consumi

Ogni run del digest è una sessione Claude Code e conta sui limiti del tuo
piano. Con `--max-turns 25` e soli strumenti di lettura il costo per run è
basso, ma se sei su un piano con limiti stretti valuta di far girare il
digest solo nei giorni lavorativi (come nel cron di esempio).
