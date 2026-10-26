import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from intelligence_center.capability_summary import automation_summary, artifact_summary, is_enabled


class CapabilitySummaryTests(unittest.TestCase):
    def test_disabled_strings_are_not_enabled(self):
        for value in (False, "false", "0", "disabled", None):
            self.assertFalse(is_enabled(value))
        for value in (True, 1, "true"):
            self.assertTrue(is_enabled(value))

    def test_enabled_is_not_running_and_records_are_not_reports(self):
        result = automation_summary([{"enabled": True}], [{"date": "2026-09-10"}], 0, "2026-09-10")
        self.assertEqual(result["label"], "1 个已启用")
        self.assertIn("最近 1 条运行记录", result["summary"])
        self.assertNotIn("生成", result["summary"])

    def test_english_failure_is_visible(self):
        result = automation_summary([{"enabled": True, "last_status": "failed"}], [], 2)
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["label"], "1 项异常")

    def test_artifact_summary_counts_all_but_today_separately(self):
        result = artifact_summary([{"date": "2026-09-10"}, {"date": "2026-09-09"}, {}], "2026-09-10")
        self.assertEqual(result["label"], "3 条记录")
        self.assertIn("今天新增 1 条", result["summary"])

    def artifact_handler(self, service):
        # Exercise the actual handler without starting channels or importing web runtime.
        path = Path(__file__).resolve().parents[1] / "channel/web/web_channel.py"
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AgentCapabilitySummaryHandler")
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_artifact_summary")
        method.decorator_list = []
        namespace = {"ArtifactsHandler": SimpleNamespace(_service=lambda: service)}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
        return namespace["_artifact_summary"]

    def test_actual_handler_uses_unfiltered_artifact_center(self):
        service = Mock()
        service.list.return_value = [{"date": "2026-09-10"}]
        result = self.artifact_handler(service)("unused-old-index-root")
        service.list.assert_called_once_with("", "all", "")
        self.assertEqual(result["label"], "1 条记录")

    def test_read_failure_does_not_claim_zero_artifacts(self):
        service = Mock()
        service.list.side_effect = RuntimeError("unavailable")
        result = self.artifact_handler(service)("unused")
        self.assertEqual(result["label"], "统计暂不可用")
        self.assertNotIn("0 条", result["summary"])


if __name__ == "__main__":
    unittest.main()
