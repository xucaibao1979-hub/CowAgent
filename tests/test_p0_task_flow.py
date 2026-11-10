"""A local replay of an actual chat-to-send-to-receipt task flow.

The fake transport prevents external messages while exercising the same task
state, trace, and evaluation code used by the running backend.
"""
from unittest.mock import Mock, patch
from types import SimpleNamespace

from agent.evaluation.runner import evaluate
from agent.execution.task_state import TaskStateStore
from channel.weixin.weixin_channel import WeixinChannel
from channel.chat_channel import ChatChannel
from common.runtime_identity import identity_scope
from common.task_trace import read_events, set_send_status


def test_unconfirmed_send_then_receiver_receipt_is_one_success(tmp_path):
    run_id = "replay-12345678"
    state_root = tmp_path / "intelligence_center" / "task_states"
    states = TaskStateStore(state_root)
    states.begin(run_id, goal="给测试会话发一条消息", source="weixin", session_id="test-user")
    channel = WeixinChannel()
    channel.api = Mock()
    channel.api.send_text.return_value = {}  # provider timed out after receiving the request
    with patch("config.get_data_root", return_value=str(tmp_path)), identity_scope(
        run_id=run_id, session_id="test-user"
    ):
        assert channel._send_text("TEST-123", "test-user", "test-token") is False
    assert states.read(run_id)["delivery"] == "awaiting_verification"
    assert states.recovery_plan(states.read(run_id)) == "verify_before_retry"
    assert channel.api.send_text.call_count == 1

    cases = [{"id": "one", "instruction": "给测试会话发一条消息", "run_id": run_id,
              "required_stages": ["微信接口发送", "设备确认收到"]}]
    events = read_events(tmp_path / "intelligence_center")
    assert evaluate(cases, events)["cases"][0]["outcome"] == "unverified"
    receipt = [{"run_id": run_id, "status": "received", "evidence_type": "test_receiver_log",
                "evidence_ref": "sha256:receipt-123", "verified_by": "test_operator"}]
    report = evaluate(cases, events, receipt)
    assert report["end_to_end_success_rate"] == 1.0
    assert report["duplicate_send_attempt_count"] == 0


def test_chat_reply_does_not_turn_unknown_send_into_failure():
    def unknown_send(_reply, _context):
        set_send_status("unconfirmed")
        return False

    fake = SimpleNamespace(channel_type="weixin", send=unknown_send)
    with patch("channel.chat_channel.emit_trace") as emit:
        ChatChannel._send(fake, None, {"channel_type": "weixin"})
    assert emit.call_args.args[2] == "unconfirmed"
