import unittest
from unittest.mock import patch
from agent.execution.task_state import TaskStateStore

from agent.permission.gateway import PermissionGateway, operation_for, risk_for


class PermissionRiskTests(unittest.TestCase):
    def test_explicit_delete_does_not_inherit_read_scope(self):
        self.assertEqual(operation_for("search_records", {"action": "delete"}), "write")
        self.assertEqual(risk_for("search_records", {"action": "delete"}), "high")

    def test_high_risk_requires_approval_even_in_full_access(self):
        decision = PermissionGateway().evaluate(mode="full-access", tool_name="delete_record", arguments={})
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.requires_approval)
        self.assertEqual(decision.risk, "high")

    def test_read_and_ordinary_send_remain_distinct(self):
        gateway = PermissionGateway()
        self.assertFalse(gateway.evaluate(mode="full-access", tool_name="search_records", arguments={}).requires_approval)
        self.assertFalse(gateway.evaluate(mode="full-access", tool_name="send_message", arguments={}).requires_approval)

    def test_high_risk_page_action_requires_approval(self):
        decision = PermissionGateway().evaluate(mode="full-access", tool_name="browser",
                                                 arguments={"action": "click", "label": "确认付款"})
        self.assertTrue(decision.requires_approval)

    def test_caller_cannot_downgrade_payment_risk(self):
        decision = PermissionGateway().evaluate(mode="full-access", tool_name="browser",
                                                 arguments={"action": "click", "label": "确认付款"},
                                                 risk="low")
        self.assertEqual(decision.risk, "high")
        self.assertTrue(decision.requires_approval)

    def test_wecom_recipient_is_bound_to_current_task(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            states = TaskStateStore(Path(root) / "intelligence_center" / "task_states")
            states.begin("weixin-target123", goal="打开企业微信，给刘伟发送你好",
                         source="weixin", session_id="test")
            with patch("config.get_data_root", return_value=root):
                wrong = PermissionGateway().evaluate(
                    mode="full-access", tool_name="desktop_wecom_send_staged_text",
                    arguments={"confirmed_contact": "张三"}, task_run_id="weixin-target123")
                right = PermissionGateway().evaluate(
                    mode="full-access", tool_name="desktop_wecom_send_staged_text",
                    arguments={"confirmed_contact": "刘伟"}, task_run_id="weixin-target123")
            self.assertFalse(wrong.allowed)
            self.assertTrue(right.allowed)


if __name__ == "__main__":
    unittest.main()
