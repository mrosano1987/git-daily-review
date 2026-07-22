"""
git_collector.py — Raccolta commit e diff dai repository Git

Effettua fetch, enumera i commit del giorno su tutti i branch,
e raccoglie i diff per l'analisi AI.
"""

import subprocess
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class CommitInfo:
    hash: str
    short_hash: str
    author: str
    email: str
    date: str
    message: str
    branch: str
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    files: list = field(default_factory=list)
    diff: str = ""


@dataclass
class RepoReport:
    name: str
    path: str
    date: str
    branches_scanned: list = field(default_factory=list)
    commits: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    fetch_ok: bool = False


def run_git(repo_path: str, args: list[str], timeout: int = 60) -> tuple[str, str, int]:
    """Esegue un comando git nel repository specificato."""
    cmd = ["git", "-C", repo_path] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace"
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", f"Timeout dopo {timeout}s: git {' '.join(args)}", 1
    except Exception as e:
        return "", str(e), 1


def fetch_all(repo_path: str) -> tuple[bool, str]:
    """Esegue git fetch --all --prune sul repository."""
    stdout, stderr, rc = run_git(repo_path, ["fetch", "--all", "--prune"])
    if rc != 0:
        return False, f"git fetch failed: {stderr}"
    return True, ""


def get_remote_branches(repo_path: str, remote: str = "origin",
                        exclude: list[str] = None) -> list[str]:
    """Restituisce la lista dei branch remoti."""
    stdout, stderr, rc = run_git(
        repo_path,
        ["branch", "-r", "--format=%(refname:short)"]
    )
    if rc != 0:
        return []

    branches = []
    exclude = exclude or []

    for line in stdout.splitlines():
        line = line.strip()
        if not line or "HEAD" in line:
            continue
        # Rimuovi il prefisso remote/ se presente
        branch_name = line.replace(f"{remote}/", "", 1) if line.startswith(f"{remote}/") else line

        # Controlla esclusioni (supporta glob semplice con *)
        excluded = False
        for pattern in exclude:
            if pattern.endswith("/*"):
                prefix = pattern[:-2]
                if branch_name.startswith(prefix):
                    excluded = True
                    break
            elif branch_name == pattern:
                excluded = True
                break

        if not excluded:
            branches.append(line)  # Mantieni il nome completo remote/branch

    return branches


def get_commits_for_date(repo_path: str, branch: str, target_date: str,
                         remote: str = "origin") -> list[CommitInfo]:
    """
    Recupera i commit di un branch per una data specifica.
    target_date: formato YYYY-MM-DD
    """
    since = f"{target_date}T00:00:00"
    until_date = datetime.strptime(target_date, "%Y-%m-%d") + timedelta(days=1)
    until = until_date.strftime("%Y-%m-%dT00:00:00")

    # Format: hash|short_hash|author|email|date|message
    log_format = "%H|%h|%an|%ae|%aI|%s"

    stdout, stderr, rc = run_git(
        repo_path,
        ["log", branch, f"--since={since}", f"--until={until}",
         f"--format={log_format}", "--no-merges"]
    )

    if rc != 0 or not stdout:
        return []

    commits = []
    branch_short = branch.replace(f"{remote}/", "", 1)

    for line in stdout.splitlines():
        parts = line.split("|", 5)
        if len(parts) < 6:
            continue

        commit = CommitInfo(
            hash=parts[0],
            short_hash=parts[1],
            author=parts[2],
            email=parts[3],
            date=parts[4],
            message=parts[5],
            branch=branch_short
        )
        commits.append(commit)

    return commits


def get_commit_stats(repo_path: str, commit_hash: str) -> dict:
    """Recupera le statistiche di un commit (file changed, insertions, deletions)."""
    stdout, stderr, rc = run_git(
        repo_path,
        ["diff-tree", "--no-commit-id", "--shortstat", "-r", commit_hash]
    )

    stats = {"files_changed": 0, "insertions": 0, "deletions": 0}

    if rc != 0 or not stdout:
        return stats

    # Parse "3 files changed, 45 insertions(+), 12 deletions(-)"
    m = re.search(r"(\d+) files? changed", stdout)
    if m:
        stats["files_changed"] = int(m.group(1))
    m = re.search(r"(\d+) insertions?", stdout)
    if m:
        stats["insertions"] = int(m.group(1))
    m = re.search(r"(\d+) deletions?", stdout)
    if m:
        stats["deletions"] = int(m.group(1))

    return stats


def get_commit_files(repo_path: str, commit_hash: str) -> list[str]:
    """Restituisce la lista dei file modificati in un commit."""
    stdout, stderr, rc = run_git(
        repo_path,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash]
    )
    if rc != 0 or not stdout:
        return []
    return [f.strip() for f in stdout.splitlines() if f.strip()]


def get_commit_diff(repo_path: str, commit_hash: str, max_lines: int = 500) -> str:
    """
    Recupera il diff di un commit, troncato a max_lines.
    Esclude file binari e file di lock.
    """
    stdout, stderr, rc = run_git(
        repo_path,
        ["diff-tree", "-p", "--no-commit-id", "-r", commit_hash,
         "--", ".", ":(exclude)*.lock", ":(exclude)package-lock.json",
         ":(exclude)yarn.lock", ":(exclude)*.min.js", ":(exclude)*.min.css"]
    )

    if rc != 0:
        return ""

    lines = stdout.splitlines()
    if len(lines) > max_lines:
        truncated = lines[:max_lines]
        truncated.append(f"\n... [troncato: {len(lines) - max_lines} righe rimanenti]")
        return "\n".join(truncated)

    return stdout


def collect_repo(repo_config: dict, target_date: str,
                 collect_diffs: bool = True,
                 min_diff_lines: int = 5) -> RepoReport:
    """
    Raccoglie tutti i commit di un repository per una data specifica.

    Args:
        repo_config: configurazione del repository da config.yaml
        target_date: data in formato YYYY-MM-DD
        collect_diffs: se raccogliere anche i diff (per analisi AI)
        min_diff_lines: soglia minima di righe per raccogliere il diff
    """
    name = repo_config["name"]
    path = os.path.expanduser(repo_config["path"])
    remote = repo_config.get("remote", "origin")
    filter_branches = repo_config.get("branches", [])
    exclude_branches = repo_config.get("exclude_branches", [])

    report = RepoReport(name=name, path=path, date=target_date)

    # Verifica che il path esista
    if not os.path.isdir(path):
        report.errors.append(f"Repository path non trovato: {path}")
        return report

    # Fetch
    ok, err = fetch_all(path)
    report.fetch_ok = ok
    if not ok:
        report.errors.append(err)
        # Procediamo comunque con i dati locali

    # Enumera branch
    all_branches = get_remote_branches(path, remote, exclude_branches)

    if filter_branches:
        # Filtra solo i branch specificati
        all_branches = [
            b for b in all_branches
            if any(fb in b for fb in filter_branches)
        ]

    report.branches_scanned = all_branches

    # Raccogli commit per ogni branch
    seen_hashes = set()  # Evita duplicati (commit presenti su più branch)

    for branch in all_branches:
        commits = get_commits_for_date(path, branch, target_date, remote)

        for commit in commits:
            if commit.hash in seen_hashes:
                continue
            seen_hashes.add(commit.hash)

            # Statistiche
            stats = get_commit_stats(path, commit.hash)
            commit.files_changed = stats["files_changed"]
            commit.insertions = stats["insertions"]
            commit.deletions = stats["deletions"]

            # Lista file
            commit.files = get_commit_files(path, commit.hash)

            # Diff (solo se supera la soglia)
            if collect_diffs and (commit.insertions + commit.deletions) >= min_diff_lines:
                commit.diff = get_commit_diff(path, commit.hash)

            report.commits.append(commit)

    # Ordina per data
    report.commits.sort(key=lambda c: c.date)

    return report


def save_report_json(report: RepoReport, output_dir: str, repo_key: str):
    """Salva il report raw in formato JSON."""
    os.makedirs(output_dir, exist_ok=True)
    filepath = os.path.join(output_dir, f"{repo_key}-commits.json")

    data = {
        "name": report.name,
        "path": report.path,
        "date": report.date,
        "fetch_ok": report.fetch_ok,
        "branches_scanned": report.branches_scanned,
        "errors": report.errors,
        "commits": [asdict(c) for c in report.commits],
        "stats": {
            "total_commits": len(report.commits),
            "total_files_changed": sum(c.files_changed for c in report.commits),
            "total_insertions": sum(c.insertions for c in report.commits),
            "total_deletions": sum(c.deletions for c in report.commits),
            "authors": list(set(c.author for c in report.commits)),
            "branches_with_commits": list(set(c.branch for c in report.commits)),
        }
    }

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return filepath
