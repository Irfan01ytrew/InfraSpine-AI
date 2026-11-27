"""Controller tests that do not use Codex credentials or PostgreSQL."""

import argparse
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import irfanix_cycle as cycle


class ControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db_path = self.root / "state.sqlite3"
        self.tasks = {
            "audit": {"id": "audit", "mode": "read", "deps": [], "task": "Audit"},
            "approval": {"id": "approval", "mode": "manual", "deps": [], "task": "Approve"},
            "release": {"id": "release", "mode": "write", "deps": ["approval"], "task": "Release"},
            "build": {"id": "build", "mode": "write", "deps": ["audit"], "task": "Build"},
        }

    def test_reopened_checkpoint_selects_next_task_but_skips_manual_gate(self):
        db = cycle.open_state(self.db_path)
        db.execute("INSERT INTO tasks(id,digest,status) VALUES (?,?,?)",
                   ("audit", cycle.digest(self.tasks["audit"]), "complete"))
        db.commit()
        db.close()
        db = cycle.open_state(self.db_path)
        self.addCleanup(db.close)
        records = {row["id"]: row for row in db.execute("SELECT * FROM tasks")}
        self.assertEqual(cycle.next_task(self.tasks, records, self.root)["id"], "build")

    def test_running_cycle_requires_reconciliation_and_preserves_exact_session(self):
        db = cycle.open_state(self.db_path)
        self.addCleanup(db.close)
        proof = "Checked child stopped and reviewed original worktree"
        db.execute("INSERT INTO tasks(id,digest,status,workspace,revision,session,pid) VALUES (?,?,?,?,?,?,?)",
                   ("audit", cycle.digest(self.tasks["audit"]), "running", str(self.root), "head", "session-1", 456))
        db.commit()
        run = argparse.Namespace(action="run", workspace=self.root, cycles=1, seconds=60)
        with self.assertRaisesRegex(ValueError, "stopped mid-cycle"):
            cycle.controller(run, self.tasks, db, self.db_path)
        reconcile = argparse.Namespace(action="resume", task="audit", evidence=proof)
        self.assertEqual(cycle.controller(reconcile, self.tasks, db, self.db_path), 0)
        row = db.execute("SELECT status,session,pid FROM tasks").fetchone()
        self.assertEqual(tuple(row), ("resume", "session-1", 456))
        with patch.object(cycle, "git", return_value="different-head"):
            with self.assertRaisesRegex(ValueError, "original HEAD"):
                cycle.run_cycle(db, self.db_path, self.tasks["audit"], self.root, row_with_workspace(db), 60)

    def test_cyclic_backlog_fails_closed(self):
        backlog = self.root / "backlog.json"
        backlog.write_text(json.dumps([{"id": "self", "mode": "read", "deps": ["self"], "task": "Cycle"}]))
        with self.assertRaisesRegex(ValueError, "cycle"):
            cycle.load_backlog(backlog)

    def test_invalid_result_cannot_complete_a_task(self):
        path = self.root / "result.json"
        path.write_text(json.dumps({"outcome": "done", "summary": "Done", "evidence": []}))
        with self.assertRaisesRegex(ValueError, "evidence"):
            cycle.result_from(path)

    def test_exclusive_lease_blocks_concurrent_executor(self):
        import sqlite3
        path = self.root / "lease.sqlite3"
        first, second = sqlite3.connect(path), sqlite3.connect(path, timeout=0)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.execute("BEGIN EXCLUSIVE")
        with self.assertRaises(sqlite3.OperationalError):
            second.execute("BEGIN EXCLUSIVE")
        first.rollback()
        second.execute("BEGIN EXCLUSIVE")

    def test_events_checkpoint_session_and_discard_raw_output(self):
        db = cycle.open_state(self.db_path)
        self.addCleanup(db.close)
        db.execute("INSERT INTO tasks(id,digest,status,attempt) VALUES (?,?,?,?)",
                   ("audit", cycle.digest(self.tasks["audit"]), "running", "attempt-1"))
        db.commit()
        stream = BytesIO(
            b"x" * (1024 * 1024 + 1) + b"\n"
            b'{"type":"thread.started","thread_id":"12345678-1234-4234-8234-123456789abc"}\n'
            b'{"type":"item.completed","item":{"output":"ordinary test text"}}\n'
            b'{"type":"turn.completed"}\n'
        )
        cycle.read_events(stream, self.db_path, "audit", "attempt-1")
        self.assertEqual(db.execute("SELECT session FROM tasks").fetchone()[0],
                         "12345678-1234-4234-8234-123456789abc")
        self.assertEqual([tuple(row) for row in db.execute("SELECT kind,detail FROM events")],
                         [("thread.started", None), ("turn.completed", None)])


def row_with_workspace(db):
    return db.execute("SELECT * FROM tasks WHERE id = 'audit'").fetchone()


if __name__ == "__main__":
    unittest.main()
