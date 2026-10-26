import unittest
from unittest.mock import patch

from plugins.intelligence_center.notifications import NotificationDispatcher, normalize_notifications


class Response:
    status=204
    def __enter__(self): return self
    def __exit__(self,*args): pass


class IntelligenceNotificationTests(unittest.TestCase):
    def setUp(self):
        persistence = patch.object(NotificationDispatcher, '_persist_events')
        persistence.start()
        self.addCleanup(persistence.stop)

    def test_multiple_targets_are_normalized(self):
        targets=normalize_notifications([
            {"type":"channel","channel":"feishu","receiver":"group-1"},
            {"type":"channel","channel":"webhook"},
            {"type":"webhook","name":"业务系统","url":"https://example.com/hook","secret":"abc"},
        ])
        self.assertEqual(len(targets),3); self.assertEqual(targets[0]["receiver"],"group-1")

    def test_invalid_manual_webhook_is_rejected(self):
        with self.assertRaisesRegex(ValueError,"地址无效"):
            normalize_notifications([{"type":"webhook","url":"javascript:alert(1)"}])

    def test_webhook_dispatch_reports_success(self):
        task={"id":"1","name":"日报","notifications":[{"type":"webhook","name":"系统","url":"https://example.com/hook","secret":"abc"}]}
        with patch("plugins.intelligence_center.notifications.safe_urlopen",return_value=Response()) as request:
            result=NotificationDispatcher().dispatch(task,"报告内容")
        self.assertTrue(result[0]["success"]); self.assertEqual(request.call_args.args[0].method,"POST")

    def test_failure_does_not_raise(self):
        task={"notifications":[{"type":"channel","channel":"missing"}]}
        result=NotificationDispatcher().dispatch(task,"报告")
        self.assertFalse(result[0]["success"])


if __name__ == "__main__": unittest.main()
