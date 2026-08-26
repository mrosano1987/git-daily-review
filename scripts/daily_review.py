#!/usr/bin/env python3
"""
daily_review.py — Script principale della Daily Code Review Routine

Orchestratore che:
1. Legge la configurazione
2. Raccoglie i commit del giorno dai repository
3. Invia i diff a Claude per l'analisi
4. Genera il report giornaliero in Markdown
5. Salva tutto nello storico

Uso:
    python daily_review.py                     # Review di oggi
    python daily_review.py --date 2026-05-12   # Review di una data specifica
    python daily_review.py --days 3            # Ultimi 3 giorni
    python daily_review.py --collect-only      # Solo raccolta, no AI
    python daily_review.py --history           # Mostra indice storico
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Aggiungi la directory degli script al path
SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

# Passa al virtualenv del progetto se questo interprete non ha PyYAML.
from _bootstrap import ensure_deps
ensure_deps()

import yaml

from git_collector import collect_repo, save_report_json
from ai_reviewer import review_commits
from report_generator import generate_report, generate_history_index
from html_report import generate_html_report
from kb_updater import extract_learnings, save_suggestions, apply_suggestions, format_suggestions_for_report


def load_dotenv(path: Path = None):
    """
    Carica variabili da un file .env nella root del progetto (se presente).
    Nessuna dipendenza esterna. Le variabili già presenti nell'ambiente
    NON vengono sovrascritte. Formato: KEY=value, righe # ignorate.
    """
    env_path = path or (ROOT_DIR / ".env")
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        pass  # best-effort: un .env malformato non deve bloccare la review


load_dotenv()

# Colori per output terminale
class C:
    HEADER = "\033[95m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    END = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"


def load_config() -> dict:
    """Carica la configurazione da config.yaml."""
    config_path = ROOT_DIR / "config" / "config.yaml"
    if not config_path.exists():
        print(f"{C.RED}Errore: config.yaml non trovato in {config_path}{C.END}")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_prerequisites(config: dict):
    """Verifica che i prerequisiti siano soddisfatti (provider-aware)."""
    errors = []

    # API key: richiesta solo per i provider cloud, con la env var corretta
    ai_config = config.get("ai", {})
    provider = ai_config.get("provider", "anthropic").lower()
    default_env_vars = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
    }
    if provider in default_env_vars:
        env_var = ai_config.get("api_key_env") or default_env_vars[provider]
        if not os.environ.get(env_var):
            errors.append(
                f"{env_var} non impostata (provider: {provider}).\n"
                f"    Opzione 1 — file .env nella root del progetto: {env_var}=...\n"
                f"    Opzione 2 — in ~/.zshrc: export {env_var}=\"...\" e poi: source ~/.zshrc"
            )
    # Ollama non richiede API key

    # Repository paths
    for key, repo in config.get("repositories", {}).items():
        path = os.path.expanduser(repo["path"])
        if not os.path.isdir(path):
            errors.append(f"Repository '{key}' non trovato: {path}")
        elif not os.path.isdir(os.path.join(path, ".git")):
            errors.append(f"'{path}' non \xe8 un repository git")

    if errors:
        print(f"\n{C.RED}{C.BOLD}Prerequisiti mancanti:{C.END}")
        for e in errors:
            print(f"  {C.RED}\u2717{C.END} {e}")
        print()
        return False
    return True

def run_review(target_date: str, config: dict, collect_only: bool = False):
    """Esegue la review per una data specifica."""
    print(f"\n{C.BOLD}{C.HEADER}{'='*60}{C.END}")
    print(f"{C.BOLD}{C.HEADER}  📋 Daily Code Review — {target_date}{C.END}")
    print(f"{C.BOLD}{C.HEADER}{'='*60}{C.END}\n")

    reports_dir = os.path.join(
        ROOT_DIR,
        config.get("reports", {}).get("output_dir", "./reports")
    )
    output_dir = os.path.join(reports_dir, target_date)
    os.makedirs(output_dir, exist_ok=True)

    ai_config = config.get("ai", {})
    config_dir = os.path.join(ROOT_DIR, "config")
    min_diff_lines = ai_config.get("min_diff_lines_for_review", 5)

    repo_reports = []
    repo_reviews = []

    for repo_key, repo_config in config.get("repositories", {}).items():
        print(f"{C.CYAN}▸ Raccolta commit: {repo_config['name']}...{C.END}")

        # Raccolta
        report = collect_repo(
            repo_config,
            target_date,
            collect_diffs=not collect_only,
            min_diff_lines=min_diff_lines
        )
        repo_reports.append(report)

        # Salva JSON raw
        json_path = save_report_json(report, output_dir, repo_key)
        print(f"  {C.GREEN}✓{C.END} {len(report.commits)} commit trovati "
              f"su {len(report.branches_scanned)} branch")

        if report.errors:
            for err in report.errors:
                print(f"  {C.YELLOW}⚠{C.END} {err}")

        # Review AI
        if not collect_only and report.commits:
            print(f"{C.CYAN}▸ Analisi AI: {repo_config['name']}...{C.END}")

            # Prepara i dati per la review
            from dataclasses import asdict
            commits_data = [asdict(c) for c in report.commits]

            review = review_commits(
                commits_data, repo_config["name"],
                config_dir, ai_config
            )
            repo_reviews.append(review)

            # Riepilogo findings
            if review.stats:
                fbs = review.stats.get("findings_by_severity", {})
                findings_str = " | ".join(
                    f"{sev}: {count}" for sev, count in fbs.items() if count > 0
                )
                if findings_str:
                    print(f"  {C.YELLOW}📝{C.END} Segnalazioni: {findings_str}")
                else:
                    print(f"  {C.GREEN}✓{C.END} Nessuna segnalazione")

                avg = review.stats.get("avg_quality_score", 0)
                if avg > 0:
                    print(f"  {C.BLUE}⭐{C.END} Quality score medio: {avg:.1f}/10")

            if review.critical_issues:
                print(f"  {C.RED}🚨 ISSUE CRITICI:{C.END}")
                for issue in review.critical_issues:
                    print(f"    {C.RED}•{C.END} {issue}")
        else:
            # Placeholder review vuota
            from ai_reviewer import RepoReview
            repo_reviews.append(RepoReview(
                repo_name=repo_config["name"],
                date=target_date,
                overall_summary="Review AI non eseguita (--collect-only)"
                if collect_only else "Nessun commit da analizzare."
            ))

    # ── Knowledge Base learning (optional) ──
    kb_suggestions = []
    kb_section = ""
    if not collect_only and repo_reviews and config.get("ai", {}).get("kb_learning", True):
        print(f"\n{C.CYAN}▸ Aggiornamento Knowledge Base...{C.END}")
        try:
            # Collect RAG contexts if available (best-effort)
            rag_contexts = []
            rag_url = config.get("rag_url", "")
            if rag_url:
                try:
                    from rag_client import retrieve_context
                    # Use a summary query for KB learning
                    all_files = [f for r in repo_reports for c in r.commits for f in c.files]
                    ctx = retrieve_context(
                        base_url=rag_url,
                        repo_name="all",
                        branch="",
                        changed_files=list(dict.fromkeys(all_files))[:30],
                        diff="",
                    )
                    if ctx.available:
                        rag_contexts = ctx.contexts
                except Exception:
                    pass

            # Extract learnings
            from dataclasses import asdict as _asdict
            ai_config = config.get("ai", {})

            # Pass serializable review data
            serializable_reviews = []
            for review in repo_reviews:
                from kb_updater import KBSuggestion
                serializable_reviews.append(review)

            kb_suggestions = extract_learnings(
                repo_reviews=repo_reviews,
                config_dir=config_dir,
                ai_config=ai_config,
                rag_contexts=rag_contexts,
            )

            if kb_suggestions:
                # Save to disk
                save_suggestions(kb_suggestions)

                # Auto-merge safe suggestions, queue the rest
                apply_result = apply_suggestions(kb_suggestions, auto_only=True)
                kb_section = format_suggestions_for_report(kb_suggestions, apply_result)

                auto = apply_result.get("merged", 0)
                queued = apply_result.get("queued", 0)
                if auto:
                    print(f"  {C.GREEN}✓{C.END} {auto} suggestion(s) auto-merged into KB")
                if queued:
                    print(f"  {C.YELLOW}⏳{C.END} {queued} suggestion(s) queued for review")
                    print(f"  {C.DIM}→ python scripts/kb_manager.py --review{C.END}")
            else:
                print(f"  {C.DIM}No new knowledge surfaced today{C.END}")

        except Exception as e:
            print(f"  {C.YELLOW}⚠{C.END} KB learning skipped: {e}")

    # Genera report Markdown
    print(f"\n{C.CYAN}▸ Generazione report...{C.END}")
    trend_days = config.get("reports", {}).get("trend_days", 7)
    report_path = generate_report(
        repo_reports, repo_reviews,
        target_date, reports_dir, trend_days,
        kb_section=kb_section,
    )
    print(f"  {C.GREEN}✓{C.END} Report salvato: {report_path}")

    # Dashboard HTML (pagina autonoma, nessuna richiesta di rete)
    try:
        dashboard_path = generate_html_report(
            repo_reports, repo_reviews,
            target_date, reports_dir, trend_days,
            kb_section=kb_section,
        )
        print(f"  {C.GREEN}✓{C.END} Dashboard: {dashboard_path}")
    except Exception as e:
        # Il Markdown è già salvato: una dashboard non generata non deve
        # far fallire la review.
        print(f"  {C.YELLOW}⚠{C.END} Dashboard HTML non generata: {e}")

    # Aggiorna indice
    index_path = generate_history_index(reports_dir)
    print(f"  {C.GREEN}✓{C.END} Indice aggiornato: {index_path}")

    # Riepilogo finale
    total_commits = sum(len(r.commits) for r in repo_reports)
    print(f"\n{C.BOLD}{C.GREEN}{'='*60}{C.END}")
    print(f"{C.BOLD}{C.GREEN}  ✅ Review completata — {total_commits} commit analizzati{C.END}")
    print(f"{C.BOLD}{C.GREEN}{'='*60}{C.END}\n")

    return report_path


def show_history(config: dict, from_date: str = None, to_date: str = None):
    """Mostra lo storico dei report."""
    reports_dir = os.path.join(
        ROOT_DIR,
        config.get("reports", {}).get("output_dir", "./reports")
    )

    index_path = generate_history_index(reports_dir)

    with open(index_path, "r") as f:
        content = f.read()

    print(content)


def main():
    parser = argparse.ArgumentParser(
        description="Daily Code Review Routine — Git Daily Review",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python daily_review.py                     # Review di oggi
  python daily_review.py --date 2026-05-12   # Data specifica
  python daily_review.py --days 3            # Ultimi 3 giorni
  python daily_review.py --collect-only      # Solo raccolta dati
  python daily_review.py --history           # Indice storico
        """
    )

    parser.add_argument(
        "--date", "-d",
        help="Data specifica da analizzare (YYYY-MM-DD)",
        default=None
    )
    parser.add_argument(
        "--days", "-n",
        type=int,
        help="Analizza gli ultimi N giorni",
        default=None
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Solo raccolta commit, senza analisi AI"
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Mostra l'indice storico dei report"
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        help="Data inizio per filtro storico (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        help="Data fine per filtro storico (YYYY-MM-DD)"
    )

    args = parser.parse_args()

    # Carica configurazione
    config = load_config()

    # Modalità storico
    if args.history:
        show_history(config, args.from_date, args.to_date)
        return

    # Verifica prerequisiti
    if not args.collect_only and not check_prerequisites(config):
        print(f"{C.YELLOW}Usa --collect-only per procedere senza API key{C.END}")
        sys.exit(1)

    # Determina date da analizzare
    if args.days:
        today = datetime.now()
        dates = [
            (today - timedelta(days=i)).strftime("%Y-%m-%d")
            for i in range(args.days - 1, -1, -1)
        ]
    elif args.date:
        dates = [args.date]
    else:
        dates = [datetime.now().strftime("%Y-%m-%d")]

    # Esegui review per ogni data
    for date in dates:
        try:
            run_review(date, config, collect_only=args.collect_only)
        except KeyboardInterrupt:
            print(f"\n{C.YELLOW}Interrotto dall'utente.{C.END}")
            sys.exit(0)
        except Exception as e:
            print(f"\n{C.RED}Errore durante la review del {date}: {e}{C.END}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()