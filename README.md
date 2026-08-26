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

python3 scripts/kb_manager.py --review             # interactive KB curation
python3 scripts/kb_manager.py --approve <ID>       # non-interactive approve
python3 scripts/kb_manager.py --reject  <ID>       # non-interactive reject
python3 scripts/kb_manager.py --stats
```

The `python3` above can be any interpreter: if it lacks the dependencies, the
entry points re-exec themselves under the project `.venv` (or `$GDR_PYTHON`),
and tell you how to create it if there isn't one. This matters for cron and
launchd, where `python3` is often the bare system Python.

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
│   ├── scheduler.py            ← launchd / cron / schtasks (no secrets on disk)
│   └── wizard_app.py           ← wizard HTTP backend
├── mcp/                        ← MCP server (TypeScript)
│   └── src/index.ts
├── templates/wizard.html
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
