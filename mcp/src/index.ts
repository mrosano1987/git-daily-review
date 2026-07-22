#!/usr/bin/env node
/**
 * git-daily-review-mcp — MCP server for Git Daily Review.
 *
 * Exposes the review engine as Model Context Protocol tools, so any MCP
 * client (Claude Code, Claude Desktop, ...) can:
 *   - run a daily code review on demand
 *   - read daily reports and the history index
 *   - list, approve, or reject knowledge-base suggestions
 *
 * The heavy lifting is delegated to the Python core in ../scripts via
 * child processes; this server is a thin, safe orchestration layer.
 *
 * Copyright (C) 2026  Git Daily Review contributors
 * Licensed under the GNU Affero General Public License v3.0 only.
 * See the LICENSE file in the repository root.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { readFile, readdir } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const execFileP = promisify(execFile);

// ── Paths ────────────────────────────────────────────────────────────────
// Repo root = parent of mcp/. Override with GDR_ROOT if the server is
// installed elsewhere.
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = process.env.GDR_ROOT ?? path.resolve(__dirname, "..", "..");
const SCRIPTS = path.join(ROOT, "scripts");
const REPORTS = path.join(ROOT, "reports");
const SUGGESTIONS = path.join(ROOT, "config", "kb_suggestions");
const PYTHON = process.env.GDR_PYTHON ?? "python3";

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ID_RE = /^[\w-]+$/;

async function runPython(
  script: string,
  args: string[],
  timeoutMs = 15 * 60 * 1000,
): Promise<string> {
  const { stdout, stderr } = await execFileP(
    PYTHON,
    [path.join(SCRIPTS, script), ...args],
    { cwd: ROOT, timeout: timeoutMs, maxBuffer: 16 * 1024 * 1024 },
  );
  return [stdout, stderr].filter(Boolean).join("\n").trim();
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }] };
}

// ── Server ───────────────────────────────────────────────────────────────
const server = new McpServer({
  name: "git-daily-review",
  version: "0.1.0",
});

server.tool(
  "run_daily_review",
  "Run the AI code review over the day's commits of all configured repositories. " +
    "Collects commits/diffs via git, reviews them against the project knowledge base, " +
    "writes a Markdown report, and (optionally) extracts knowledge-base suggestions. " +
    "May take several minutes depending on commit volume and the configured model.",
  {
    date: z
      .string()
      .regex(DATE_RE, "YYYY-MM-DD")
      .optional()
      .describe("Specific date to review (YYYY-MM-DD). Default: today."),
    days: z
      .number()
      .int()
      .min(1)
      .max(31)
      .optional()
      .describe("Review the last N days instead of a single date."),
    collect_only: z
      .boolean()
      .optional()
      .describe("Only collect git data, skip the AI analysis."),
  },
  async ({ date, days, collect_only }) => {
    const args: string[] = [];
    if (date) args.push("--date", date);
    if (days) args.push("--days", String(days));
    if (collect_only) args.push("--collect-only");
    const out = await runPython("daily_review.py", args);
    return textResult(out || "Review completed (no output).");
  },
);

server.tool(
  "get_report",
  "Read the full Markdown daily report for a given date (findings, quality scores, " +
    "critical issues, KB updates).",
  {
    date: z.string().regex(DATE_RE, "YYYY-MM-DD").describe("Report date (YYYY-MM-DD)."),
  },
  async ({ date }) => {
    const file = path.join(REPORTS, date, "daily-summary.md");
    if (!existsSync(file)) {
      return textResult(
        `No report found for ${date}. Use list_reports to see available dates, ` +
          `or run_daily_review to generate it.`,
      );
    }
    return textResult(await readFile(file, "utf-8"));
  },
);

server.tool(
  "list_reports",
  "List the available daily reports (dates) and return the history index if present.",
  {},
  async () => {
    if (!existsSync(REPORTS)) return textResult("No reports directory yet.");
    const entries = (await readdir(REPORTS, { withFileTypes: true }))
      .filter((e) => e.isDirectory() && DATE_RE.test(e.name))
      .map((e) => e.name)
      .sort()
      .reverse();
    const indexPath = path.join(REPORTS, "INDEX.md");
    const index = existsSync(indexPath) ? await readFile(indexPath, "utf-8") : "";
    const header = entries.length
      ? `Available reports (${entries.length}): ${entries.join(", ")}`
      : "No reports generated yet.";
    return textResult([header, index].filter(Boolean).join("\n\n---\n\n"));
  },
);

server.tool(
  "list_kb_suggestions",
  "List knowledge-base suggestions extracted from past reviews, optionally filtered " +
    "by status (pending suggestions await human approval).",
  {
    status: z
      .enum(["pending", "merged", "rejected", "approved"])
      .optional()
      .describe("Filter by lifecycle status. Default: all."),
  },
  async ({ status }) => {
    if (!existsSync(SUGGESTIONS)) return textResult("No suggestions directory yet.");
    const files = (await readdir(SUGGESTIONS)).filter((f) => f.endsWith(".yaml"));
    if (files.length === 0) return textResult("No knowledge-base suggestions yet.");
    const blocks: string[] = [];
    for (const f of files.sort()) {
      const raw = await readFile(path.join(SUGGESTIONS, f), "utf-8");
      if (status && !new RegExp(`^status:\\s*${status}\\b`, "m").test(raw)) continue;
      blocks.push(`### ${f}\n\`\`\`yaml\n${raw.trim()}\n\`\`\``);
    }
    return textResult(
      blocks.length
        ? blocks.join("\n\n")
        : `No suggestions with status '${status}'.`,
    );
  },
);

server.tool(
  "decide_kb_suggestion",
  "Approve (merge into the knowledge base) or reject a pending knowledge-base " +
    "suggestion by its ID. Approval permanently updates config/knowledge-base.yaml.",
  {
    id: z.string().regex(ID_RE).describe("Suggestion ID, e.g. 20260518_142233_00."),
    action: z.enum(["approve", "reject"]).describe("Decision to apply."),
  },
  async ({ id, action }) => {
    const flag = action === "approve" ? "--approve" : "--reject";
    const out = await runPython("kb_manager.py", [flag, id], 60_000);
    return textResult(out);
  },
);

server.tool(
  "kb_stats",
  "Show knowledge-base statistics: rule counts per layer, suggestion history, KB version.",
  {},
  async () => textResult(await runPython("kb_manager.py", ["--stats"], 60_000)),
);

// ── Boot ─────────────────────────────────────────────────────────────────
const transport = new StdioServerTransport();
await server.connect(transport);
console.error(`git-daily-review MCP server ready (root: ${ROOT})`);
