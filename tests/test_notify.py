"""Test di scripts/notify.py: testi con soli esiti e conteggi, agganci in agent_run, watchdog."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_run  # noqa: E402
import notify  # noqa: E402

DAY = "2026-11-04"
T0 = datetime(2026, 11, 4, 17, 14)
CFG = {"label": "gdr-test"}


class NotifyTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        agent_run.RUNS_DIR = tmp / "runs"
        agent_run.NOTIFY_ENABLED = True
        notify.REPORTS_DIR = tmp / "reports"
        notify.AUTOFIX_STATE = tmp / "autofix.json"
        (notify.REPORTS_DIR / DAY).mkdir(parents=True)
        (notify.REPORTS_DIR / DAY / "dashboard-data.json").write_text(json.dumps({
            "totals": {"commits": 9}, "quality_avg": 8.25,
            "findings_by_severity": {"critical": 1, "error": 2, "warning": 3, "info": 4},
            "authors": {"Mario Rossi": {}}, "repos": [{"name": "client_secret_repo"}]}))
        notify.AUTOFIX_STATE.write_text(json.dumps({"attempts": {
            "client_secret_repo@abc": {"status": "pushed", "run_day": DAY, "branch": "autofix/x",
                                        "findings": [{"message": "testo riservato"}]},
            "b@1": {"status": "skipped", "run_day": DAY}, "c@2": {"status": "approved", "run_day": DAY},
            "d@3": {"status": "published", "run_day": "2026-11-01"}}}))
        self.sent = []
        notify.load_config = lambda: CFG
        notify.send = lambda text, cfg=None: (self.sent.append(text) or True, "inviata")

    def test_summary_has_only_outcomes_and_counts(self):
        agent_run.begin(DAY, "claude", now=T0)
        msg = agent_run.end(DAY, "claude", "partial", note="RAG giù su client_secret_repo",
                            warnings=["rag", "bogus"], now=T0 + timedelta(minutes=38))
        self.assertIn("notifica inviata", msg)
        text = self.sent[-1]
        for expected in ("⚠️ gdr-test · 2026-11-04 · claude", "17:14 → 17:52", "9 commit", "8.2/10",
                         "1 critical, 2 error, 3 warning", "1 branch pubblicati", "1 saltati", "1 in sospeso",
                         "RAG non disponibile"):
            self.assertIn(expected, text)
        for leaked in ("client_secret_repo", "Mario Rossi", "testo riservato", "autofix/x", "bogus"):
            self.assertNotIn(leaked, text)

    def test_takeover_is_notified(self):
        agent_run.begin(DAY, "claude", now=T0)
        agent_run.begin(DAY, "antigravity", stale_hours=2, now=T0 + timedelta(hours=2, minutes=16))
        self.assertIn("subentra antigravity", self.sent[-1])
        self.assertIn("17:14", self.sent[-1])

    def test_watchdog(self):
        self.assertIn("Nessuna run è partita", notify.watchdog_text(DAY, CFG))
        agent_run.begin(DAY, "claude", now=T0)
        self.assertIn("ancora aperta", notify.watchdog_text(DAY, CFG))
        agent_run.end(DAY, "claude", now=T0 + timedelta(minutes=30))
        self.assertEqual(notify.watchdog_text(DAY, CFG), "")

    def test_notification_failure_never_breaks_the_run(self):
        def boom(text, cfg=None):
            raise RuntimeError("rete giù")
        notify.send = boom
        agent_run.begin(DAY, "claude", now=T0)
        self.assertIn("notifica non inviata", agent_run.end(DAY, "claude", now=T0 + timedelta(minutes=5)))
        self.assertTrue(agent_run.load(DAY)["done"])


class SendTest(unittest.TestCase):
    def test_unconfigured(self):
        import importlib
        fresh = importlib.reload(notify)
        self.assertEqual(fresh.send("x", {})[0], False)
        self.assertIn("non configurate", fresh.send("x", {})[1])


if __name__ == "__main__":
    unittest.main()
