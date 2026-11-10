import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.tools.audit import classify_error
from common.runtime_identity import identity_scope
from common.task_trace import emit, read_events


class DeliveryDiagnosticsTests(unittest.TestCase):
    def test_delivery_failures_have_distinct_codes(self):
        self.assertEqual(classify_error("微信会话发送令牌已失效 ret=-14"), "session_token_expired")
        self.assertEqual(classify_error("微信接口准备发送失败 ret=-2"), "provider_prepare_failed")
        self.assertEqual(classify_error("消息通道不可用"), "channel_unavailable")

    def test_one_run_id_connects_route_tool_and_send(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch("config.get_data_root", return_value=temporary):
                with identity_scope(run_id="wx-run-1", session_id="wx-session"):
                    emit(None, "选择执行路径", "selected", intent="desktop", route="desktop_open_app")
                    emit(None, "工具调用", "completed", tool="desktop_open_app", duration_ms=25)
                    emit(None, "微信接口发送", "accepted", notification_written=True)
            events = read_events(Path(temporary) / "intelligence_center")
        self.assertEqual([item["stage"] for item in events], ["选择执行路径", "工具调用", "微信接口发送"])
        self.assertEqual({item["id"] for item in events}, {"wx-run-1"})
        self.assertEqual(events[1]["duration_ms"], 25)


if __name__ == "__main__":
    unittest.main()
