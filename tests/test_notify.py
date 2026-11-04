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
    """Invio reale sostituito: `_call` registra o fallisce come una rete che blocca il canale."""

    def setUp(self):
        import importlib
        self.n = importlib.reload(notify)
        self.n.OUTBOX = Path(tempfile.mkdtemp()) / "outbox.jsonl"
        self.n.read_token = lambda *a: "123:token"
        self.cfg = {"telegram": {"chat_id": "@c", "keychain_service": "x"}}
        self.calls, self.offline = [], False

        def call(token, method, params=None):
            if self.offline:
                raise self.n.NotifyError("Telegram non raggiungibile (ConnectionResetError)")
            self.calls.append(params["text"])
            return {"ok": True}
        self.n._call = call

    def test_unconfigured(self):
        self.assertEqual(self.n.send("x", {}), (False, "notifiche non configurate (notify.teams o notify.telegram)"))
        self.assertFalse(self.n.OUTBOX.exists())

    def test_missing_chat_id_waits_in_queue(self):
        ok, detail = self.n.send("esito", {"telegram": {"chat_id": "", "keychain_service": "x"}})
        self.assertFalse(ok)
        self.assertIn("chat_id", detail)
        self.assertEqual(self.n.send("", self.cfg), (True, "inviata su telegram (+1 dalla coda)"))
        self.assertTrue(self.calls[0].startswith("esito\n(in ritardo"))

    def test_offline_messages_wait_in_queue(self):
        self.offline = True
        ok, detail = self.n.send("primo", self.cfg)
        self.assertFalse(ok)
        self.assertIn("in coda", detail)
        self.n.send("secondo", self.cfg)
        self.assertEqual([i["text"] for i in self.n._queued()], ["primo", "secondo"])
        self.offline = False
        self.assertEqual(self.n.send("terzo", self.cfg), (True, "inviata su telegram (+2 dalla coda)"))
        self.assertTrue(self.calls[0].startswith("primo\n(in ritardo"))
        self.assertEqual(self.calls[2], "terzo")
        self.assertFalse(self.n.OUTBOX.exists())

    def test_flush_only(self):
        self.offline = True
        self.n.send("vecchio", self.cfg)
        self.offline = False
        self.assertTrue(self.n.send("", self.cfg)[0])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.n.send("", self.cfg), (True, "nessun messaggio da inviare"))


class TeamsTest(unittest.TestCase):
    """Workflow Teams: card Adaptive, URL trattato come segreto, coda come per Telegram."""

    def setUp(self):
        import importlib
        self.n = importlib.reload(notify)
        self.n.OUTBOX = Path(tempfile.mkdtemp()) / "outbox.jsonl"
        self.url = "https://flow.example.invalid/workflows/1?sig=SEGRETO"
        self.n.read_token = lambda *a: self.url
        self.cfg = {"teams": {"keychain_service": "x"}, "telegram": {"chat_id": "@c"}}
        self.posts, self.offline = [], False

        def post(url, text):
            if self.offline:
                raise self.n.NotifyError("Teams non raggiungibile (TimeoutError)")
            self.posts.append((url, text))
        self.n._post_teams = post

    def test_teams_is_preferred_and_queues_offline(self):
        self.offline = True
        self.assertIn("in coda", self.n.send("esito", self.cfg)[1])
        self.offline = False
        self.assertEqual(self.n.send("altro", self.cfg), (True, "inviata su teams (+1 dalla coda)"))
        self.assertEqual(self.posts[0][0], self.url)
        self.assertEqual(self.posts[1][1], "altro")

    def test_channel_can_be_chosen(self):
        self.assertEqual(self.n._channel({**self.cfg, "channel": "telegram"})[0], "telegram")
        self.assertEqual(self.n._channel({"channel": "teams"}), (None, {}))

    def test_bad_url_is_not_printed(self):
        self.url = "http://flow.example.invalid/?sig=SEGRETO"
        ok, detail = self.n.send("esito", self.cfg)
        self.assertFalse(ok)
        self.assertIn("non valido", detail)
        self.assertNotIn("SEGRETO", detail)
        self.url = "https://flow.example.invalid/powerautomate/automations/direct/wo"
        self.assertIn("senza firma", self.n.send("esito", self.cfg)[1])

    def test_card_has_one_block_per_line(self):
        card = self.n.teams_card("✅ titolo\nriga 1\nriga 2")
        body = card["attachments"][0]["content"]["body"]
        self.assertEqual([b["text"] for b in body], ["✅ titolo", "riga 1", "riga 2"])
        self.assertEqual(body[0]["weight"], "Bolder")


if __name__ == "__main__":
    unittest.main()
