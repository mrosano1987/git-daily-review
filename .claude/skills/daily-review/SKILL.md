---
name: daily-review
description: Operate the git-daily-review engine in this repository. Use when the user asks to run a code review of the day's commits, read or summarize a daily review report, check the review history or quality trend, or list/approve/reject knowledge-base suggestions. Also used by the scheduled daily digest routine.
---

# Daily Review — operating guide

This project contains git-daily-review: an engine that reviews the day's git
commits against the project knowledge base and writes Markdown reports.
Your job is to operate it and turn its output into decisions.

## Commands (run from the repository root)

Run a review:
- Today: `python3 scripts/daily_review.py`
- Specific date: `python3 scripts/daily_review.py --date YYYY-MM-DD`
- Last N days: `python3 scripts/daily_review.py --days N`
- Git data only (fast, no AI): add `--collect-only`

Skipped runs: every scheduled run first recovers skipped workdays (no
report, failed `git fetch`, or run before the scheduled hour) in ONE run
over the whole interval, up to 6 without asking; older ones are flagged in
today's report as awaiting confirmation. When a digest is written and
today's report contains "🔁 Controllo run saltate", carry that note into
the digest (what was recovered, which runs await a decision). Commands:
`--catch-up-check` (plan only), `--catch-up-only [--catch-up-all]`,
`--catch-up-ignore-before YYYY-MM-DD`, `--no-catch-up`. Never run
`--catch-up-all` or `--catch-up-ignore-before` autonomously: they are the
user's decision.

Reports:
- Read `reports/<YYYY-MM-DD>/daily-summary.md` for a day's findings
- Read `reports/INDEX.md` for the history index and quality trend

Every review run also writes, in the same day folder:
- `dashboard.html` — a self-contained graphical dashboard (hero quality score,
  KPI tiles, severity strip, trend/gate/author charts, filterable commit list,
  a table view behind every chart). No CDN, no network calls: the client data
  stays local. Open it in a browser to triage a day visually.
- `dashboard-data.json` — the serialized model the dashboard is rendered from.

Rebuild the dashboard without re-running the AI (needed after writing the
digest, so the digest appears inside the page):
`python3 scripts/html_report.py --date YYYY-MM-DD`

Web portal (all reports, history, git-flow, custom dashboards):
`python3 scripts/portal.py` → http://127.0.0.1:8765. Local only, read-only
on reports/KB/repos. Point the user here when they want to browse or
aggregate across days rather than read a single report. The portal reads
from `data/review.db` (SQLite), which every review run and the digest's
`html_report.py` step update; `python3 scripts/review_db.py --sync` imports
anything new (e.g. a briefing), `--stats` shows what it holds. Briefings
are stored parsed (table `briefings`: digest date, preparation date,
sections, open question) and have their own "Briefing" view in the portal;
save new ones as `reports/<digest-date>/briefing.md` so they are picked up. Never commit
`data/` — it holds client data.

Knowledge base curation:
- List suggestions: `python3 scripts/kb_manager.py --list [--status pending]`
- Approve one: `python3 scripts/kb_manager.py --approve <ID>`
- Reject one: `python3 scripts/kb_manager.py --reject <ID>`
- Stats: `python3 scripts/kb_manager.py --stats`

If the git-daily-review MCP server is connected, prefer its tools
(run_daily_review, get_report, list_kb_suggestions, decide_kb_suggestion)
over raw commands — same engine, structured output.

## Rules

1. NEVER edit `config/knowledge-base.yaml` directly. All KB changes go
   through suggestions and `kb_manager.py --approve/--reject`, which keep
   versioning and changelog consistent.
2. A review can take several minutes with a local Ollama model — that is
   normal. Do not kill the process early.
3. If the run fails with a provider/connection error, check that Ollama is
   running (`curl -s http://localhost:11434/api/tags`) before retrying.
4. When approving KB suggestions autonomously (e.g. in a scheduled run):
   only approve suggestions that are BOTH confidence >= 0.9 AND additive
   conventions with no side effects. Anything touching quality gates with
   severity critical/error, domain rules, or integrations: leave pending
   and flag it for human review. When in doubt, leave it pending.
5. Never commit or expose `config/config.yaml`, `config/knowledge-base.yaml`,
   `.env`, or anything under `reports/` — they may contain client context.

## Digest format

When asked to summarize a report (or when running as the daily digest
routine), produce — in the user's language:

1. **Headline**: one line — commits reviewed, repos, average quality score
   and trend vs. the previous report in INDEX.md (better/worse/stable)
2. **Critical & error findings**: each with file, one-line explanation, and
   the violated gate ID (e.g. SEC-001). If none: say so explicitly.
3. **Per-author note**: only if a pattern repeats for the same author
   (mentoring signal, keep it constructive and factual)
4. **Pending KB suggestions**: ID + one-line reason each, with your
   approve/reject recommendation and why
5. **One recommended action** for the team lead for tomorrow

Keep the digest under ~300 words. No filler, no praise padding. If the
report for the requested date does not exist, offer to run the review
instead of inventing content.

After writing `digest.md`, always run
`python3 scripts/html_report.py --date <YYYY-MM-DD>` so the digest is
embedded in that day's `dashboard.html`. Report both paths (digest and
dashboard) as the output of the routine.
