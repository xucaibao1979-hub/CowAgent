import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from agent.execution.run_journal import RunJournal
from agent.tools.scheduler.scheduler_service import SchedulerService
from agent.tools.scheduler.task_store import TaskStore


class RunJournalTests(unittest.TestCase):
    def test_unknown_send_result_requires_verification(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RunJournal(Path(root) / "run_checkpoints")
            journal.begin("scheduled-12345678", "task-1", "send_message")
            self.assertEqual(journal.recovery_plan(journal.read("scheduled-12345678")), "safe_retry")
            journal.step("scheduled-12345678", "send_started", "running")
            self.assertEqual(journal.recovery_plan(journal.read("scheduled-12345678")), "needs_verification")

    def test_trace_receipt_prevents_duplicate_retry(self):
        checkpoint = {"status": "running", "mode": "scheduled", "action_type": "send_message", "steps": []}
        trace = [{"stage": "微信接口发送", "status": "accepted"}]
        self.assertEqual(RunJournal.recovery_plan(checkpoint, trace), "needs_verification")

    def test_scheduler_recovers_only_pre_send_reminder(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(str(Path(root) / "tasks.json"))
            store.add_task({"id": "task-1", "name": "提醒", "enabled": True,
                            "next_run_at": (datetime.now() - timedelta(minutes=1)).isoformat(),
                            "action": {"type": "send_message"}, "schedule": {"type": "daily", "time": "07:30"}})
            service = SchedulerService(store, lambda task: True)
            journal = service._run_journal()
            guard = Path(root) / "run_windows" / "safe.json"
            guard.parent.mkdir()
            guard.write_text("{}", encoding="utf-8")
            journal.begin("scheduled-12345678", "task-1", "send_message", guard_path=str(guard))
            with patch("common.task_trace.read_events", return_value=[]):
                service._recover_interrupted_runs()
            self.assertFalse(guard.exists())
            self.assertEqual(store.get_task("task-1")["last_run_status"], "recovery_scheduled")
            self.assertEqual(journal.read("scheduled-12345678")["status"], "recovery_scheduled")

    def test_scheduler_does_not_replay_started_send(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(str(Path(root) / "tasks.json"))
            store.add_task({"id": "task-2", "name": "提醒", "enabled": True,
                            "next_run_at": datetime.now().isoformat(),
                            "action": {"type": "send_message"}, "schedule": {"type": "daily", "time": "07:30"}})
            service = SchedulerService(store, lambda task: True)
            journal = service._run_journal()
            journal.begin("scheduled-87654321", "task-2", "send_message")
            journal.step("scheduled-87654321", "send_started", "running")
            with patch("common.task_trace.read_events", return_value=[]), \
                 patch.object(service, "_calculate_next_run", return_value=datetime.now() + timedelta(days=1)):
                service._recover_interrupted_runs()
            self.assertEqual(store.get_task("task-2")["last_run_status"], "interrupted_unverified")
            self.assertEqual(journal.read("scheduled-87654321")["status"], "interrupted_unverified")


if __name__ == "__main__":
    unittest.main()
