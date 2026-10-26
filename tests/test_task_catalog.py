import unittest

from intelligence_center.task_catalog import UnifiedTaskCatalog, normalize_scheduler_task


class _Automation:
    def list_tasks(self):
        return {"tasks": [{"id": "a1", "name": "新闻", "kind": "news", "enabled": True, "definition": {}}]}


class _Scheduler:
    def list_tasks(self):
        return [{"id": "s1", "name": "提醒", "enabled": False, "schedule": {"type": "cron", "expression": "0 8 * * *"}, "action": {"type": "send_message", "content": "早报", "channel_type": "web"}}]


class UnifiedTaskCatalogTest(unittest.TestCase):
    def test_catalog_normalizes_both_task_origins(self):
        result = UnifiedTaskCatalog(_Automation(), _Scheduler()).list()
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["summary"], {"automation": 1, "scheduler": 1, "enabled": 1})
        scheduled = next(x for x in result["tasks"] if x["origin"] == "scheduler")
        self.assertEqual(scheduled["uid"], "scheduler:s1")
        self.assertEqual(scheduled["definition"]["trigger"]["type"], "cron")
        self.assertNotIn("webhook_url", str(scheduled["definition"]))


if __name__ == "__main__":
    unittest.main()
