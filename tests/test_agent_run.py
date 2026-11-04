"""Test di scripts/agent_run.py: una sola run completa al giorno, con subentro della riserva."""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_run  # noqa: E402

DAY = "2026-11-02"
T0 = datetime(2026, 11, 2, 17, 10)


class AgentRunTest(unittest.TestCase):
    def setUp(self):
        agent_run.RUNS_DIR = Path(tempfile.mkdtemp())
        agent_run.NOTIFY_ENABLED = False

    def test_backup_skips_completed_day(self):
        self.assertEqual(agent_run.begin(DAY, "claude", now=T0)[0], agent_run.PROCEED)
        agent_run.end(DAY, "claude", now=T0 + timedelta(minutes=40))
        code, msg = agent_run.begin(DAY, "antigravity", now=T0 + timedelta(hours=2))
        self.assertEqual(code, agent_run.DONE)
        self.assertIn("claude", msg)

    def test_backup_waits_for_run_in_progress(self):
        agent_run.begin(DAY, "claude", now=T0)
        self.assertEqual(agent_run.begin(DAY, "antigravity", now=T0 + timedelta(hours=1))[0], agent_run.RUNNING)

    def test_backup_takes_over_stale_run(self):
        agent_run.begin(DAY, "claude", now=T0)
        code, msg = agent_run.begin(DAY, "antigravity", now=T0 + timedelta(hours=5))
        self.assertEqual(code, agent_run.PROCEED)
        self.assertIn("interrotta", msg)
        agent_run.end(DAY, "antigravity", "partial", now=T0 + timedelta(hours=6))
        data = agent_run.load(DAY)
        self.assertEqual(data["done"]["harness"], "antigravity")
        self.assertEqual([a["harness"] for a in data["attempts"]], ["claude", "antigravity"])


if __name__ == "__main__":
    unittest.main()
