# 🔍 Git Daily Review

**Automated daily AI code review for any Git repository — usable as a CLI, a scheduled routine, or an MCP server for AI agents.**

Git Daily Review analyzes your daily commits across multiple repositories, checks them against your project's conventions and quality gates, and produces a structured Markdown report. It ships with an **MCP (Model Context Protocol) server**, so agents like Claude Code and Claude Desktop can run reviews, read reports, and curate the project knowledge base conversationally.

License: **AGPL-3.0-only** · Python 3.10+ core · TypeScript MCP server

---

## Highlights

- **Any repo, any language** — the AI adapts to your stack (TypeScript, Python, Java, Go, Rust, C#, …)
- **Multi-repo** — monitor as many repositories as you need in a single run
- **MCP server** — expose reviews, reports, and knowledge-base curation as agent tools
- **Multi-provider AI** — Anthropic Claude, OpenAI, Google Gemini, or any local model via Ollama (fully offline)
- **Self-improving knowledge base** — 7-layer project context that learns from each review, with a code-enforced auto-merge policy and human approval for high-impact changes
- **Markdown + HTML output** — every run writes `daily-summary.md` and a self-contained `dashboard.html` (quality trend, gate and per-author charts, filterable commit list, a table view behind every chart). No CDN, no network calls: your code review data never leaves the machine
- **Web portal** — a local, navigable interface over every report collected: a default reviewer dashboard plus custom ones you build from widgets, commit and review history with diffs, per-day reports, a git-flow graph of the real branch topology, release timeline from version tags, author mentoring signals and the KB queue
- **RAG-ready** — optional integration with an external RAG service for semantic context
- **Cross-platform scheduling** — macOS (launchd), Linux (cron), Windows (Task Scheduler); secrets stay in `.env`, never in the scheduler files
- **Setup wizard** — web-based configuration, no manual YAML editing required

---

## Quick start (CLI)

```bash
git clone https://github.com/YOUR_USER/git-daily-review.git
cd git-daily-review

# 0. Dependencies in a virtualenv (setup.py does this for you if you skip it)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. Configure (opens the wizard in your browser)
python3 setup.py

# 2. Secrets (cloud providers only — Ollama needs none)
cp .env.example .env      # then add e.g. ANTHROPIC_API_KEY=...
chmod 600 .env

# 3. First review
python3 scripts/daily_review.py

# 4. Schedule daily runs
python3 scripts/scheduler.py --install
```

CLI reference:

```bash
python3 scripts/daily_review.py                    # review today
python3 scripts/daily_review.py --date 2026-05-12  # specific date
python3 scripts/daily_review.py --days 5           # last N days
python3 scripts/daily_review.py --collect-only     # git data only, no AI
python3 scripts/daily_review.py --history          # report history index

python3 scripts/daily_review.py --catch-up-check   # show skipped runs and the recovery plan
python3 scripts/daily_review.py --catch-up-only    # recover skipped runs only (no review of today)
python3 scripts/daily_review.py --catch-up-only --catch-up-all   # also the ones beyond the limit of 6
python3 scripts/daily_review.py --catch-up-ignore-before 2026-09-24  # stop flagging older skipped runs
python3 scripts/daily_review.py --no-catch-up      # skip the preventive check

python3 scripts/portal.py                          # web portal on http://127.0.0.1:8765
python3 scripts/review_db.py --sync                # import new report files into the database
python3 scripts/review_db.py --rebuild             # rebuild the database from reports/
python3 scripts/review_db.py --stats

python3 scripts/kb_manager.py --review             # interactive KB curation
python3 scripts/kb_manager.py --approve <ID>       # non-interactive approve
python3 scripts/kb_manager.py --reject  <ID>       # non-interactive reject
python3 scripts/kb_manager.py --stats
```

### Skipped runs are recovered automatically

Every scheduled run (no `--date`/`--days`) starts with a preventive check of
the last 30 days of working days (Mon–Fri). A run counts as **skipped** when
that day has no report, when its `git fetch` failed (incomplete data), or when
it ran before the scheduled hour (it only saw the morning's commits). A day
is covered by a valid run of its own or by a recovery run that included it.

Skipped runs are recovered with **one** run over the continuous interval from
the oldest one to recover until today 00:00, excluding commits already
reviewed (from the review database). The report goes into the folder of the
last skipped day, with the covered interval and the recovered runs in its
header; today's report notes what was recovered.

At most **6** runs are recovered without asking (the most recent). Beyond
that, an interactive run asks for confirmation; an unattended run recovers the
6 and leaves the older ones *awaiting confirmation*: they are flagged in
today's report (so in the digest and briefing) until you recover them
(`--catch-up-only --catch-up-all`) or dismiss them
(`--catch-up-ignore-before`). The recovery is postponed if `git fetch` fails.
State lives in `data/catch_up.json` (git-ignored).

The `python3` above can be any interpreter: if it lacks the dependencies, the
entry points re-exec themselves under the project `.venv` (or `$GDR_PYTHON`),
and tell you how to create it if there isn't one. This matters for cron and
launchd, where `python3` is often the bare system Python.

---

## Web portal

```bash
python3 scripts/portal.py                 # opens http://127.0.0.1:8765
python3 scripts/portal.py --port 9000 --no-browser
```

A single local page (Python standard library + vanilla JS, SVG charts, no CDN,
listening on `127.0.0.1` only) backed by a local SQLite database:

| View | What it shows |
|------|---------------|
| **Dashboard** | Default *reviewer overview* (KPIs with delta vs. previous period, quality trend, commits per branch type, findings per severity, top violated gates, quality per author, directory hotspots, commit-time heatmap, findings to discuss). Create custom dashboards — blank or from the *Author mentoring* / *Releases & stability* templates — and add widgets: KPI, line, columns, stacked columns, horizontal ranking, heatmap, commit or finding tables. Each widget picks a metric (commits, quality, findings, critical+error, churn, files, % reviewed, authors, lines per commit), a grouping (day/week/month, author, repo, branch, branch type, gate, severity, directory, file, weekday, hour, quality band), an optional split and its own filters. |
| **Daily reports** | Coverage per day (failed `git fetch` flagged), then per-day KPIs, charts, the digest, briefing and full report rendered, and a link to that day's `dashboard.html`. |
| **Briefing** | Every dev-team briefing, current and earlier versions, by preparation date: the five points as sections, the open question highlighted, links to the source digest; digests left without a briefing are flagged, and a table collects the open questions asked to the team over time. |
| **Commit history** | Every collected commit with its review outcome; search, filter, sort; a drawer with findings, files and the diff (falls back to `git show` on the local clone when the diff was not saved). |
| **Authors** | Quality, findings by severity, average commit size and the recurring gate per author (mentoring signal). |
| **Git-flow** | Lane graph of the real topology from the local clone (`git log --all`, never `fetch`): branches coloured by type (main, develop, release, hotfix, feature, bugfix), merges, tags, and review score on reviewed commits; weekly activity by branch type and review coverage. |
| **Releases** | Version tags with the number of commits each release introduced and the days between releases. |
| **KB suggestions** | Read-only view of the suggestion queue; decisions still go through `kb_manager.py`. |

Global filters (period, repository, author) apply to every view.

### Review database

`data/review.db` (SQLite, git-ignored, override with `$GDR_DB`) archives what
the routines produce: collected commits with diffs, review outcomes and
findings, per-repo collection status and AI summaries, the day's documents
(`daily-summary.md`, `digest.md`, `briefing.md`, …) plus the briefings parsed
into structure (digest date, preparation date, sections, open question —
table `briefings`), the KB suggestion queue
and the custom dashboards. It is fed in three ways:

- `daily_review.py` imports the day at the end of every run;
- `html_report.py --date` (the step after the digest) re-imports the day, so the digest lands too;
- the portal runs an incremental sync (file mtimes under `reports/` and
  `config/kb_suggestions/`) at most every 5 s while in use, so documents
  written by agents — such as the morning briefing — show up with no extra hook.

`reports/` stays the source of truth: `review_db.py --rebuild` recreates the
database from it (custom dashboards are kept). Days stay in the database even
if their report folder is later deleted. Git-flow and releases are still read
live from the local clones.

---

## MCP server (use it as an agent)

The `mcp/` directory contains a TypeScript MCP server that wraps the review engine.

```bash
cd mcp
npm install
npm run build
```

Register it with **Claude Code**:

```bash
claude mcp add git-daily-review -- node /absolute/path/to/git-daily-review/mcp/dist/index.js
```

Or add it to **Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "git-daily-review": {
      "command": "node",
      "args": ["/absolute/path/to/git-daily-review/mcp/dist/index.js"]
    }
  }
}
```

Exposed tools:

| Tool | What it does |
|------|--------------|
| `run_daily_review` | Run the review for today, a date, or the last N days |
| `get_report` | Read the full Markdown report for a date |
| `list_reports` | List available reports + history index |
| `list_kb_suggestions` | List KB suggestions (filter by status) |
| `decide_kb_suggestion` | Approve/reject a pending suggestion by ID |
| `kb_stats` | Knowledge-base statistics |

Then just ask your agent things like *“run today's review and summarize the critical issues”* or *“show me pending KB suggestions and approve the ones about naming conventions”*.

Environment overrides: `GDR_ROOT` (project root, default: repo root), `GDR_PYTHON` (Python executable; default: the project `.venv` if present, otherwise `python3`).

---

## AI providers

| Provider | Models | API key env var | Notes |
|----------|--------|-----------------|-------|
| **Anthropic** | Claude Haiku, Sonnet, Opus | `ANTHROPIC_API_KEY` | |
| **OpenAI** | GPT-4o-mini, GPT-4o, o1-mini | `OPENAI_API_KEY` | |
| **Google Gemini** | Gemini 2.0 Flash, 1.5 Flash/Pro | `GEMINI_API_KEY` | |
| **Ollama** (local) | any pulled model | *(none)* | fully offline; set `num_ctx` in config (default 32768) to avoid prompt truncation |

Secrets live in a `.env` file at the project root (see `.env.example`), loaded automatically at startup. They are **never** written into launchd plists, crontabs, or batch files.

---

## Knowledge base (7 layers)

`config/knowledge-base.yaml` gives the reviewer structured project context:

```
L0 Identity · L1 Architecture · L2 Conventions · L3 Quality Gates
L4 Domain · L5 Integrations · L6 Release
```

After each run the system extracts *new knowledge* surfaced by the review and stores it as suggestions in `config/kb_suggestions/`. The auto-merge policy is **enforced by code** (not by the model): only new conventions and info/warning gates with confidence ≥ 0.85 can auto-merge, capped at 2 per run. Critical gates, domain rules, and integrations always require human approval — interactively (`kb_manager.py --review`), via CLI flags, or through the MCP `decide_kb_suggestion` tool.

Disable learning entirely with `kb_learning: false` in the `ai:` section of `config.yaml`.

---

## Project structure

```
git-daily-review/
├── setup.py                    ← setup wizard entry point
├── requirements.txt
├── .env.example                ← secrets template (never commit .env)
├── config/
│   ├── config.example.yaml
│   └── knowledge-base.example.yaml
├── scripts/                    ← Python core
│   ├── daily_review.py         ← orchestrator (loads .env at startup)
│   ├── git_collector.py        ← fetch, commits, diffs
│   ├── ai_reviewer.py          ← review pipeline
│   ├── ai_provider.py          ← Anthropic/OpenAI/Ollama/Gemini abstraction
│   ├── kb_updater.py           ← KB learning + code-enforced auto-merge policy
│   ├── kb_manager.py           ← KB curation CLI (interactive + --approve/--reject)
│   ├── rag_client.py           ← optional RAG client
│   ├── report_generator.py     ← Markdown reports + history index
│   ├── html_report.py          ← self-contained HTML dashboard (charts, filters)
│   ├── catch_up.py             ← skipped-run detection and single-run recovery
│   ├── portal.py               ← local web portal (HTTP server, read-only API)
│   ├── review_db.py            ← SQLite review database: schema, import from reports, queries
│   ├── portal_data.py          ← git-flow layout, releases, git show fallback
│   ├── scheduler.py            ← launchd / cron / schtasks (no secrets on disk)
│   └── wizard_app.py           ← wizard HTTP backend
├── mcp/                        ← MCP server (TypeScript)
│   └── src/index.ts
├── templates/
│   ├── wizard.html
│   └── portal.html             ← portal single-page app
└── reports/                    ← generated daily reports (git-ignored)
```

---

## Roadmap

- [ ] Slack/email notifications for the daily report
- [ ] `--ci` mode: non-zero exit on critical findings (PR gating)
- [ ] Full TypeScript port of the core
- [ ] Remote repository support (review without a local clone)

Contributions welcome — open an issue or a PR.

---

## License

This project is licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0-only). If you run a modified version as a network service, you must offer its source to the users of that service. See [LICENSE](LICENSE).
