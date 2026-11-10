from pathlib import Path
import unittest

from agent.evaluation.runner import evaluate, grade_run, link_recorded_runs, load_cases, load_receipts
from agent.evaluation.collect import collect
from agent.execution.task_state import TaskStateStore


class TaskEvaluationTests(unittest.TestCase):
    def test_seed_cases_are_distinct_and_routable(self):
        cases = load_cases(Path(__file__).parents[1] / "agent" / "evaluation" / "cases.json")
        report = evaluate(cases, [])
        self.assertEqual(len(cases), 50)
        self.assertGreaterEqual(report["routing_accuracy"], 0.95)
        self.assertEqual(report["not_run"], 50)
        self.assertIsNone(report["end_to_end_success_rate"])


    def test_interface_acceptance_is_not_delivery(self):
        events = [{"stage": "微信接口发送", "status": "accepted"}]
        self.assertEqual(grade_run(events, ["微信接口发送"])["outcome"], "accepted")
        self.assertEqual(grade_run(events, ["设备确认收到"])["outcome"], "unverified")

    def test_unknown_send_then_verified_receipt_counts_as_success(self):
        cases = [{"id": "wx-test", "instruction": "发给测试微信", "run_id": "wx-12345678",
                  "required_stages": ["微信接口发送", "设备确认收到"]}]
        events = [{"id": "wx-12345678", "stage": "微信接口发送", "status": "unconfirmed"}]
        before = evaluate(cases, events)
        self.assertEqual(before["cases"][0]["outcome"], "unverified")
        receipt = [{"run_id": "wx-12345678", "status": "received",
                    "evidence_type": "test_phone_screenshot", "evidence_ref": "sha256:abc",
                    "verified_by": "test_operator"}]
        after = evaluate(cases, events, receipt)
        self.assertEqual(after["cases"][0]["outcome"], "success")
        self.assertEqual(after["end_to_end_success_rate"], 1.0)

    def test_receipt_for_unknown_run_cannot_create_success(self):
        cases = [{"id": "wx-test", "instruction": "发给测试微信", "run_id": "wx-12345678"}]
        receipt = [{"run_id": "wx-12345678", "status": "received",
                    "evidence_type": "test_phone_screenshot", "evidence_ref": "sha256:abc",
                    "verified_by": "test_operator"}]
        assert evaluate(cases, [], receipt)["not_run"] == 1

    def test_duplicate_send_attempts_are_reported(self):
        events = [{"stage": "微信接口发送", "status": "unconfirmed", "artifact_id": "same"},
                  {"stage": "微信接口发送", "status": "accepted", "artifact_id": "same"}]
        assert grade_run(events)["duplicate_send_attempts"] == 1

    def test_receipt_requires_external_evidence_reference(self):
        with self.assertRaises(ValueError):
            load_receipts(Path(__file__).parents[1] / "agent" / "evaluation" / "cases.json")

    def test_real_case_links_only_to_same_goal_and_session(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            store = TaskStateStore(root)
            store.begin("weixin-12345678", goal="查上海天气", source="weixin", session_id="user-a")
            cases = [{"id": "a", "instruction": "查上海天气", "source_session": "user-a"},
                     {"id": "b", "instruction": "查上海天气", "source_session": "user-b"}]
            linked = link_recorded_runs(cases, root)
            self.assertEqual(linked[0]["run_id"], "weixin-12345678")
            self.assertNotIn("run_id", linked[1])


    def test_false_success_and_manual_intervention_are_counted(self):
        events = [{"stage": "生成回复", "status": "completed", "duration_ms": 10},
                  {"stage": "微信接口发送", "status": "failed", "duration_ms": 25},
                  {"stage": "人工接管", "status": "needs_user"}]
        result = grade_run(events)
        self.assertTrue(result["false_success"])
        self.assertEqual(result["manual_interventions"], 1)
        self.assertEqual(result["duration_ms"], 35)

    def test_real_instructions_are_unscored_until_reviewed(self):
        class Store:
            def list_sessions(self, **kwargs):
                return {"sessions": [{"session_id": "s1"}]}

            def load_history_page(self, *args, **kwargs):
                return {"messages": [{"role": "user", "content": "帮我查找桌面文件", "created_at": 1},
                                     {"role": "user", "content": "我的密码是12345678", "created_at": 2}]}

        cases = collect(Store())
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["annotation_status"], "unreviewed")
        report = evaluate(cases, [])
        self.assertIsNone(report["routing_accuracy"])
