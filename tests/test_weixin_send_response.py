import unittest
from unittest.mock import Mock, patch
import requests

from channel.weixin.weixin_channel import WeixinChannel
from channel.weixin.weixin_api import WeixinApi


class WeixinSendResponseTests(unittest.TestCase):
    def test_prepare_failed_keeps_cached_token(self):
        channel = WeixinChannel()
        channel._invalidate_context_token = Mock()

        self.assertFalse(channel._check_send_response({"ret": -2, "errmsg": "prepare failed"}, "receiver"))
        channel._invalidate_context_token.assert_not_called()
        self.assertIn("准备发送失败", channel._last_send_error)

    def test_session_expired_invalidates_cached_token(self):
        channel = WeixinChannel()
        channel._invalidate_context_token = Mock()

        self.assertFalse(channel._check_send_response({"ret": -14}, "receiver"))
        channel._invalidate_context_token.assert_called_once_with("receiver")

    def test_invalid_provider_response_is_not_success(self):
        channel = WeixinChannel()
        self.assertFalse(channel._check_send_response(None, "receiver"))
        self.assertIn("未返回有效", channel._last_send_error)
        self.assertFalse(channel._check_send_response({}, "receiver"))
        self.assertIn("未返回发送状态", channel._last_send_error)
        self.assertEqual(channel._last_send_status, "unconfirmed")

    def test_send_timeout_is_unconfirmed_not_accepted(self):
        api = WeixinApi(token="test-token")
        with patch("channel.weixin.weixin_api.requests.post", side_effect=requests.exceptions.Timeout):
            response = api.send_text("receiver", "hello", "context")
        self.assertEqual(response, {})
        channel = WeixinChannel()
        self.assertFalse(channel._check_send_response(response, "receiver"))
        self.assertEqual(channel._last_send_status, "unconfirmed")

    def test_trace_marks_empty_response_unconfirmed(self):
        channel = WeixinChannel()
        channel.api = Mock()
        channel.api.send_text.return_value = {}
        with patch("common.task_trace.emit") as emit:
            self.assertFalse(channel._send_text("hello", "receiver", "context"))
        self.assertEqual(emit.call_args.args[2], "unconfirmed")

    def test_send_connection_loss_does_not_retry_possible_delivery(self):
        api = WeixinApi(token="test-token")
        with patch("channel.weixin.weixin_api.requests.post",
                   side_effect=requests.exceptions.ConnectionError) as request:
            response = api.send_text("receiver", "hello", "context")
        self.assertEqual(response, {})
        request.assert_called_once()

    def test_media_rejection_returns_failure(self):
        channel = WeixinChannel()
        channel.api = Mock()
        channel.api.send_file_item.return_value = {"ret": -2, "errmsg": "prepare failed"}
        channel._resolve_media_path = Mock(return_value="C:/temp/report.pdf")
        upload = {"encrypt_query_param": "q", "aes_key_b64": "key", "raw_size": 12}
        with patch("channel.weixin.weixin_channel.upload_media_to_cdn", return_value=upload):
            self.assertFalse(channel._send_file("report.pdf", "receiver", "token"))
        self.assertIn("准备发送失败", channel._last_send_error)


if __name__ == "__main__":
    unittest.main()
