"""Test di scripts/autofix.py su repository git finti (origin bare in locale) e bot finto.

    python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import autofix  # noqa: E402
import review_db  # noqa: E402

DAY = "2026-11-02"


def sh(cwd, *args):
    return subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class AutofixTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        origin, self.repo = self.tmp / "origin.git", self.tmp / "app"
        sh(self.tmp, "git", "init", "--bare", "-b", "develop", str(origin))
        sh(self.tmp, "git", "clone", "-q", str(origin), str(self.repo))
        for k, v in (("user.name", "Test"), ("user.email", "t@example.com")):
            sh(self.repo, "git", "config", k, v)
        sh(self.repo, "git", "checkout", "-q", "-b", "develop")
        (self.repo / "page.ts").write_text("export function f() {\n  return 1;\n}\n")
        sh(self.repo, "git", "add", ".")
        sh(self.repo, "git", "commit", "-qm", "init")
        sh(self.repo, "git", "push", "-q", "origin", "develop")
        sh(self.repo, "git", "checkout", "-q", "-b", "feature/x")
        (self.repo / "page.ts").write_text("export function f() {\n  console.log('debug');\n  return 1;\n}\n")
        sh(self.repo, "git", "commit", "-qam", "Add f logging")
        sh(self.repo, "git", "push", "-q", "origin", "feature/x")
        self.hash = sh(self.repo, "git", "rev-parse", "HEAD")
        self.origin = origin

        review_db.DB_PATH = self.tmp / "review.db"
        with review_db.connect() as conn:
            conn.execute("INSERT INTO repos(name, path) VALUES ('app', ?)", (str(self.repo),))
            conn.execute("""INSERT INTO commits(repo, hash, short, author, message, branch, first_day, last_day)
                            VALUES ('app', ?, ?, 'Dev', 'Add f logging', 'feature/x', ?, ?)""",
                         (self.hash, self.hash[:9], DAY, DAY))
            for rule, sev in (("TS-002", "error"), ("GIT-003", "error"), ("DOC-001", "info")):
                conn.execute("""INSERT INTO findings(repo, hash, day, rule_id, severity, file, line, message)
                                VALUES ('app', ?, ?, ?, ?, 'page.ts', '2', 'msg ' || ?)""",
                             (self.hash, DAY, rule, sev, rule))

        self.reviewer = self.tmp / "bot.py"
        self.set_verdict("APPROVATO")
        autofix.CONFIG_PATH = self.tmp / "autofix.yaml"
        autofix.CONFIG_PATH.write_text(textwrap.dedent(f"""
            worktrees_dir: {self.tmp / 'wt'}
            reviewer:
              command: ["{sys.executable}", "{self.reviewer}", "{{profile}}"]
            repos:
              app:
                reviewer_profile: app-profile
                pr: {{host: none}}
        """))
        autofix.STATE_PATH = self.tmp / "autofix.json"
        autofix.REPORTS_DIR = self.tmp / "reports"
        self.cfg = autofix.load_config()
        self.cid = autofix.candidate_id("app", self.hash)

    def set_verdict(self, verdict, status="answered", extra=""):
        self.reviewer.write_text(
            "import sys\n"
            f"print('[{status} #7 · profilo ' + sys.argv[1] + ']')\n"
            f"print('Analisi della correzione. {extra}')\n"
            f"print('VERDETTO: {verdict}')\n")

    def fix(self, content="export function f() {\n  return 1;\n}\n"):
        wt = Path(autofix.prepare(self.cfg, self.cid, DAY)["worktree"])
        (wt / "page.ts").write_text(content)
        sh(wt, "git", "commit", "-qam", "Remove console.log")
        return wt

    # ── candidati ──────────────────────────────────────────────
    def test_candidates_filter_severity_and_process_rules(self):
        cands = autofix.find_candidates(self.cfg, DAY)
        self.assertEqual([c["id"] for c in cands], [self.cid])
        self.assertEqual([f["rule_id"] for f in cands[0]["findings"]], ["TS-002"])

    def test_closed_candidates_disappear(self):
        autofix.close(self.cfg, self.cid, DAY, "skipped", "falso positivo")
        self.assertEqual(autofix.find_candidates(self.cfg, DAY), [])

    def test_old_findings_out_of_window(self):
        self.assertEqual(autofix.find_candidates(self.cfg, "2026-11-20"), [])

    # ── percorso completo ──────────────────────────────────────
    def test_happy_path_pushes_autofix_branch_only(self):
        self.fix()
        self.assertTrue(autofix.check(self.cfg, self.cid)["ok"])
        self.assertTrue(autofix.review(self.cfg, self.cid, "rimuove il log di debug")["approved"])
        self.assertEqual(autofix.publish(self.cfg, self.cid, DAY, dry_run=True)["blocks"], [])
        res = autofix.publish(self.cfg, self.cid, DAY)
        self.assertEqual(res["status"], "pushed")
        branch = f"autofix/{DAY}-{self.hash[:9]}"
        self.assertIn(branch, sh(self.origin, "git", "branch", "--list", "autofix/*"))
        # il branch del commit originale su origin non cambia
        self.assertEqual(sh(self.origin, "git", "rev-parse", "feature/x"), self.hash)
        report = autofix.write_report(DAY).read_text()
        self.assertIn("pushed", report)
        # senza PR automatica la descrizione resta pronta da incollare
        draft = Path(autofix.load_state()["attempts"][self.cid]["pr_draft"])
        self.assertIn("Revisione di bot", draft.read_text())
        self.assertIn(draft.name, report)

    def test_bitbucket_without_token_gives_link(self):
        att = {"repo_path": str(self.repo), "branch": "autofix/x", "base_branch": "feature/x"}
        url, problem = autofix._open_pr_bitbucket(att, self.cfg, {"workspace": "ws", "repo_slug": "app"}, "t", "b")
        self.assertIsNone(url)
        self.assertIn("https://bitbucket.org/ws/app/pull-requests/new?source=autofix%2Fx&dest=feature%2Fx", problem)

    # ── varchi ─────────────────────────────────────────────────
    def test_publish_blocked_without_approval(self):
        self.fix()
        autofix.check(self.cfg, self.cid)
        self.set_verdict("MODIFICHE")
        self.assertFalse(autofix.review(self.cfg, self.cid, "r")["approved"])
        self.assertTrue(autofix.publish(self.cfg, self.cid, DAY)["blocks"])

    def test_new_commit_after_approval_needs_new_review(self):
        wt = self.fix()
        autofix.check(self.cfg, self.cid)
        autofix.review(self.cfg, self.cid, "r")
        (wt / "page.ts").write_text("export function f() {\n  return 2;\n}\n")
        sh(wt, "git", "commit", "-qam", "sneaky change")
        blocks = autofix.publish(self.cfg, self.cid, DAY)["blocks"]
        self.assertTrue(any("check" in b for b in blocks))
        self.assertTrue(any("approvazione" in b for b in blocks))

    def test_escalated_or_low_confidence_is_not_approval(self):
        self.fix()
        autofix.check(self.cfg, self.cid)
        self.set_verdict("APPROVATO", status="escalated")
        self.assertFalse(autofix.review(self.cfg, self.cid, "r")["approved"])
        autofix.check(self.cfg, self.cid)
        self.set_verdict("APPROVATO", extra="Confidenza: bassa.")
        self.assertFalse(autofix.review(self.cfg, self.cid, "r")["approved"])

    def test_review_rounds_are_capped(self):
        self.fix()
        autofix.check(self.cfg, self.cid)
        self.set_verdict("MODIFICHE")
        autofix.review(self.cfg, self.cid, "r")
        autofix.review(self.cfg, self.cid, "r")
        with self.assertRaises(autofix.AutofixError):
            autofix.review(self.cfg, self.cid, "r")

    def test_forbidden_files_fail_check(self):
        wt = Path(autofix.prepare(self.cfg, self.cid, DAY)["worktree"])
        (wt / ".env").write_text("X=1\n")
        sh(wt, "git", "add", "-f", ".env")
        sh(wt, "git", "commit", "-qm", "oops")
        res = autofix.check(self.cfg, self.cid)
        self.assertFalse(res["ok"])
        self.assertTrue(any(".env" in p for p in res["problems"]))

    def test_daily_pr_cap(self):
        self.cfg["max_prs_per_day"] = 0
        self.fix()
        autofix.check(self.cfg, self.cid)
        autofix.review(self.cfg, self.cid, "r")
        self.assertTrue(any("massimo" in b for b in autofix.publish(self.cfg, self.cid, DAY)["blocks"]))

    def use_fallback(self, verdict="APPROVATO"):
        """Revisore principale senza credito, riserva in formato agy-json."""
        backup = self.tmp / "agy.py"
        backup.write_text(
            "import json, sys\n"
            "print(json.dumps({'status': 'SUCCESS', 'conversation_id': 'c1',"
            f" 'response': 'Contesto verificato. ' + sys.argv[-1][:10] + ' VERDETTO: {verdict}'}}))\n")
        self.cfg["reviewer"]["fallbacks"] = [{"name": "riserva", "format": "agy-json", "preamble": "Portale {profile}. ",
                                              "command": [sys.executable, str(backup)]}]
        self.reviewer.write_text("print('[error · limite di spesa raggiunto]')\n")

    def test_fallback_reviewer_when_primary_unavailable(self):
        self.use_fallback()
        self.fix()
        autofix.check(self.cfg, self.cid)
        res = autofix.review(self.cfg, self.cid, "r")
        self.assertTrue(res["approved"])
        self.assertEqual(res["reviewer"], "riserva")
        text = Path(res["file"]).read_text()
        self.assertIn("Revisori non disponibili", text)
        self.assertIn("Contesto verificato. Portale ap", text)     # il preambolo arriva al revisore
        self.assertEqual(autofix.publish(self.cfg, self.cid, DAY, dry_run=True)["blocks"], [])

    def test_no_fallback_after_a_real_verdict(self):
        self.use_fallback()
        self.set_verdict("MODIFICHE")
        self.fix()
        autofix.check(self.cfg, self.cid)
        res = autofix.review(self.cfg, self.cid, "r")
        self.assertFalse(res["approved"])
        self.assertEqual(res["reviewer"], "bot")

    def test_parse_agy_json(self):
        ok = '{"status": "SUCCESS", "conversation_id": "x", "response": "ok\\nVERDETTO: APPROVATO"}'
        self.assertTrue(autofix.parse_review(ok, "agy-json")["approved"])
        self.assertFalse(autofix.parse_review('{"status": "ERROR"}', "agy-json")["approved"])
        self.assertFalse(autofix.parse_review("not json", "agy-json")["approved"])

    def test_parse_review(self):
        out = "[answered #3 · profilo p]\nok\n\n━━ BOZZA #3 (costo $0.10) ━━\nAnalisi…\n**VERDETTO: APPROVATO**\n💡 hint"
        self.assertTrue(autofix.parse_review(out)["approved"])
        self.assertEqual(autofix.parse_review(out)["request_id"], "3")
        self.assertFalse(autofix.parse_review("[refused · x]\nlimite")["approved"])

    def test_question_is_single_line_safe(self):
        att = {"repo": "app", "base_branch": "feature/x", "hash": self.hash, "commit_message": "m",
               "findings": [{"severity": "error", "rule_id": "TS-002", "file": "a.ts", "line": "2",
                             "message": "console.log"}]}
        q = autofix.build_question(att, "-a\n+b\n" * 10, "perché", 20)
        self.assertIn("¶", q)
        self.assertIn("diff troncato", q)
        self.assertFalse(q.startswith(("/", "!")))


if __name__ == "__main__":
    unittest.main()
