import io
import json
import unittest
from unittest.mock import patch

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.channel_factory import create_channel
from channel.webhook.webhook_channel import WebhookChannel


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class WebhookChannelTests(unittest.TestCase):
    def test_factory_creates_webhook_channel(self):
        self.assertIsInstance(create_channel("webhook"), WebhookChannel)

    def test_startup_rejects_invalid_url(self):
        with patch("channel.webhook.webhook_channel.conf", return_value={"webhook_url": "javascript:bad"}):
            with self.assertRaises(ValueError):
                WebhookChannel().startup()

    def test_send_posts_json_and_signature(self):
        config = {"webhook_url": "https://example.com/hook", "webhook_secret": "secret", "webhook_timeout": 8}
        context = Context(ContextType.TEXT, "", {"receiver": "ops-team"})
        with patch("channel.webhook.webhook_channel.conf", return_value=config), patch(
            "urllib.request.urlopen", return_value=_Response(b"ok")
        ) as request_mock:
            WebhookChannel().send(Reply(ReplyType.TEXT, "任务完成"), context)

        request = request_mock.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual("任务完成", payload["content"])
        self.assertEqual("ops-team", payload["receiver"])
        self.assertTrue(request.headers["X-cowagent-signature"].startswith("sha256="))
        self.assertEqual(8, request_mock.call_args.kwargs["timeout"])


if __name__ == "__main__":
    unittest.main()
