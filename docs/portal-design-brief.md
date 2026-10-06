# Design brief — Git Daily Review portal

Brief for a design pass in Claude Design (claude.ai/design). It describes the
portal as implemented in `templates/portal.html`; it contains **no client
data** — use the placeholder data below, never real reports.

## Product

A local web portal for a **tech lead / code reviewer** who reads AI code
reviews of the team's daily commits. Jobs to be done:

1. In 30 seconds each morning: is quality going up or down, is anything
   critical, is the data complete (did the git fetch fail)?
2. Prepare the dev-team briefing: findings to discuss, per-author mentoring
   signals, KB suggestions to decide.
3. Understand how the repository evolves: branches, merges, releases.
4. Build their own views by aggregating the data the way they need.

## Information architecture

Sidebar (sections) + sticky top bar with global filters
(period presets 7/30/90 days/All + from–to dates, repository, author, theme).

| Section | Content |
|---|---|
| Dashboard | Default "Reviewer overview" (read-only, can be duplicated) + user dashboards listed under it in the sidebar |
| Daily reports | Coverage chart, cards per day grouped by month → day detail: 4 KPIs, 2 charts, tabs Digest / Briefing / Full report / Commits |
| Commit history | Search + filters, sortable table, side drawer with findings, files, diff |
| Authors | Two charts + mentoring table (recurring gate per author) |
| Git-flow | Lane graph (SVG) aligned with a commit table; branch-type toggles; activity and coverage cards |
| Releases | Per repo: commits per release (columns) + table of latest tags |
| KB suggestions | Status KPIs + read-only table |

## Dashboard widget system

Widget = type × metric × grouping × optional split × own filters × width
(¼, ⅓, ½, ⅔, full on a 12-column grid).

- Types: KPI tile (value + delta vs previous period), line over time,
  columns over time, stacked columns, horizontal ranking bars,
  weekday×hour heatmap, commit table, findings table.
- Every chart has a hover tooltip and a "table view" toggle.
- Edit mode: dashed outline on cards, per-card ↑ ↓ ⚙ ✕, "+ Widget" opens a
  modal form. Templates: blank, "Author mentoring", "Releases & stability".

Design asks: a better widget-builder modal (live preview?), drag-to-reorder
and resize affordances, an empty state for a new dashboard, a clearer
distinction between the built-in and user dashboards.

## Visual system (keep)

- System sans only; tabular numbers in tables and axes.
- Categorical palette in fixed order — blue `#2a78d6`, orange `#eb6834`,
  aqua `#1baf7a`, yellow `#eda100`, magenta `#e87ba4`, green `#008300`,
  violet `#4a3aa7`, red `#e34948` (dark mode has its own validated steps).
- Branch types map to fixed slots: main=blue, develop=orange,
  release=aqua, hotfix=red, feature=violet, bugfix=green, other=grey.
- Status colours are reserved for severity and always paired with an icon
  and a label: critical `#d03b3b` ⛔, error `#ec835a` ✖, warning `#fab219` ▲,
  info grey ℹ, clean `#0ca30c` ✓.
- Single-hue blue ramp for the heatmap. Thin marks, 4px rounded bar ends,
  2px gaps between stacked segments, 2px lines, no dual axes.
- Surfaces: plane `#f9f9f7` / card `#fcfcfb` (light); `#0d0d0d` / `#1a1a19` (dark).

## Constraints

- Must stay a single self-contained HTML file: no CDN, no web fonts, no
  external requests (client data, local-only tool).
- Desktop first (1280–1600px); usable down to tablet.
- Italian UI copy.

## Placeholder data for mockups

Repos `app-mobile`, `app-mobile-v2`; authors Anna B., Luca R., Marta S.,
Paolo V.; ~70 commits over 11 weeks; quality 7.8–9.1/10; findings 23×GIT-001,
16×DOC-001, 8×TS-001, 4×TS-002, 1×SEC-001 (critical); tags v8.9.0 … v8.11.0.
