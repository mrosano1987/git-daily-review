#!/usr/bin/env bash
# daily-routine.sh — Git Daily Review, routine giornaliera in due fasi:
#   FASE 1: motore Python (deterministico, locale, gratis con Ollama)
#   FASE 2: Claude Code headless legge il report e produce il digest
#
# Installazione: vedi ROUTINE-SETUP.md in questa cartella.
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────
PROJECT_DIR="${GDR_PROJECT_DIR:-$HOME/Progetti/git-daily-review}"
LOG_DIR="$PROJECT_DIR/logs"
TODAY="$(date +%F)"
DIGEST_FILE="$PROJECT_DIR/reports/$TODAY/digest.md"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"   # path assoluto se cron non lo trova

# Interprete: il venv del progetto se c'è, altrimenti il python3 del PATH
# (che sotto cron può essere quello di sistema, senza le dipendenze).
if [ -n "${GDR_PYTHON:-}" ]; then
  PY="$GDR_PYTHON"
elif [ -x "$PROJECT_DIR/.venv/bin/python3" ]; then
  PY="$PROJECT_DIR/.venv/bin/python3"
else
  PY="python3"
fi

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR"

# ── FASE 1: review engine ────────────────────────────────────────────────
echo "[$(date '+%F %T')] Fase 1: engine run ($PY)" >> "$LOG_DIR/routine.log"
"$PY" scripts/daily_review.py >> "$LOG_DIR/routine.log" 2>&1 || {
  echo "[$(date '+%F %T')] ❌ Engine fallito, salto il digest" >> "$LOG_DIR/routine.log"
  exit 1
}

# Nessun report di oggi = nessun commit: fine, non spendere token
if [ ! -f "reports/$TODAY/daily-summary.md" ]; then
  echo "[$(date '+%F %T')] Nessun report per oggi (0 commit?), stop." >> "$LOG_DIR/routine.log"
  exit 0
fi

# ── FASE 2: digest agentico con Claude headless ──────────────────────────
# Sola lettura + kb_manager: la skill 'daily-review' del progetto detta
# formato del digest e policy di approvazione KB.
echo "[$(date '+%F %T')] Fase 2: Claude digest" >> "$LOG_DIR/routine.log"
"$CLAUDE_BIN" -p "Usa la skill daily-review: leggi reports/$TODAY/daily-summary.md \
e reports/INDEX.md, poi scrivi il digest giornaliero nel formato previsto \
dalla skill, in italiano. Elenca anche le KB suggestion pending con la tua \
raccomandazione (non approvare nulla autonomamente). Salva il digest in \
reports/$TODAY/digest.md" \
  --allowedTools "Read,Grep,Glob,Write(reports/**),Bash(python3 scripts/kb_manager.py --list*),Bash(python3 scripts/kb_manager.py --stats),Bash(.venv/bin/python3 scripts/kb_manager.py --list*),Bash(.venv/bin/python3 scripts/kb_manager.py --stats)" \
  --permission-mode dontAsk \
  --max-turns 25 \
  >> "$LOG_DIR/routine.log" 2>&1 || {
  echo "[$(date '+%F %T')] ⚠️ Digest fallito (engine ok, report disponibile)" >> "$LOG_DIR/routine.log"
  exit 0
}

# ── FASE 3: ri-genera la dashboard includendo il digest ──────────────────
# Deterministico e fuori dall'agente: nessuna analisi AI, solo re-render di
# dashboard.html da dashboard-data.json + digest.md.
"$PY" scripts/html_report.py --date "$TODAY" >> "$LOG_DIR/routine.log" 2>&1 || {
  echo "[$(date '+%F %T')] ⚠️ Dashboard non rigenerata (digest ok)" >> "$LOG_DIR/routine.log"
}

echo "[$(date '+%F %T')] ✅ Routine completata: $DIGEST_FILE" >> "$LOG_DIR/routine.log"

# ── Opzionale: notifica ──────────────────────────────────────────────────
# macOS, popup a fine run (decommentare):
# osascript -e "display notification \"Digest pronto: reports/$TODAY\" with title \"Git Daily Review\""
# Slack via webhook (decommentare e mettere l'URL in .env come SLACK_WEBHOOK_URL):
# [ -n "${SLACK_WEBHOOK_URL:-}" ] && curl -s -X POST -H 'Content-type: application/json' \
#   --data "{\"text\":$(python3 -c "import json,sys;print(json.dumps(open('$DIGEST_FILE').read()))")}" \
#   "$SLACK_WEBHOOK_URL" > /dev/null
