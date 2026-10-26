import unittest
import uuid
from pathlib import Path

from plugins.intelligence_center.store import AutomationTask, TaskStore
from plugins.intelligence_center.templates import TEMPLATES


class IntelligenceCenterTests(unittest.TestCase):
    def setUp(self):
        self.test_directory = Path("work/test-data") / uuid.uuid4().hex
        self.test_directory.mkdir(parents=True)

    def tearDown(self):
        for path in sorted(self.test_directory.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.test_directory.rmdir()

    def test_eight_initial_templates_are_available(self):
        self.assertEqual(8, len(TEMPLATES))
        self.assertIn("news_digest", TEMPLATES)
        self.assertIn("ops_report", TEMPLATES)

    def test_task_lifecycle_is_isolated_and_persistent(self):
        path = self.test_directory / "intelligence" / "tasks.json"
        store = TaskStore(str(path))
        task = AutomationTask(
            name="AI 行业每日资讯",
            template_id="news_digest",
            keywords=["AI Agent", "MCP"],
            schedule="每天08:30",
        )

        store.create(task)
        self.assertTrue(path.exists())
        self.assertEqual(task.id, store.list()[0].id)

        toggled = store.toggle(task.id)
        self.assertFalse(toggled.enabled)

        store.delete(task.id)
        self.assertEqual([], store.list())

    def test_missing_task_operations_fail_explicitly(self):
        store = TaskStore(str(self.test_directory / "tasks.json"))
        with self.assertRaises(KeyError):
            store.toggle("missing")
        with self.assertRaises(KeyError):
            store.delete("missing")


if __name__ == "__main__":
    unittest.main()
