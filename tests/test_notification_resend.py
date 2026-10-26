import unittest
from copy import deepcopy
from pathlib import Path
import uuid
import shutil
from unittest.mock import Mock, patch

from plugins.intelligence_center.automation_runs import AutomationRunService
from intelligence_center.search_config_audit import inspect_search_task


class ResendTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'work' / 'resend-tests' / uuid.uuid4().hex
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        self.root = root
        report = root / 'report.md'
        report.write_text('Original report', encoding='utf-8')
        self.service = AutomationRunService(root)
        self.data = {'runs': [{'id': 'one', 'status': 'success', 'report_path': str(report),
            'task_snapshot': {'notifications': [{'channel': 'web', 'receiver': 'a'}, {'channel': 'web', 'receiver': 'b'}]},
            'notification_results': [{'target': 'web', 'channel': 'web', 'receiver': 'a', 'success': True},
                                     {'target': 'web', 'channel': 'web', 'receiver': 'b', 'success': False}]}]}
        self.service._read = lambda: deepcopy(self.data)
        self.service._write = lambda data: setattr(self, 'data', deepcopy(data))
        self.service._register_artifact = Mock()

    @patch('plugins.intelligence_center.notifications.NotificationDispatcher.dispatch')
    def test_only_failed_target_and_original_report(self, dispatch):
        dispatch.return_value = [{'target': 'web', 'channel': 'web', 'receiver': 'b', 'success': True}]
        result = self.service.resend_notifications('one')
        self.assertEqual(result['resent'], 1)
        task, content = dispatch.call_args.args
        self.assertEqual(content, 'Original report')
        self.assertEqual([x['receiver'] for x in task['notifications']], ['b'])
        self.assertEqual(self.data['runs'][0]['status'], 'success')
        self.assertFalse(self.data['runs'][0]['notification_failure'])
        self.assertEqual(self.service.resend_notifications('one')['resent'], 0)
        dispatch.assert_called_once()

    @patch('plugins.intelligence_center.notifications.NotificationDispatcher.dispatch')
    def test_unknown_send_never_automatically_repeated(self, dispatch):
        dispatch.side_effect = RuntimeError('connection lost')
        with self.assertRaises(ValueError): self.service.resend_notifications('one')
        with self.assertRaises(ValueError): self.service.resend_notifications('one')
        dispatch.assert_called_once()

    @patch('plugins.intelligence_center.notifications.NotificationDispatcher.dispatch')
    def test_missing_report_and_running_rejected(self, dispatch):
        self.data['runs'][0]['report_path'] = str(self.root / 'missing')
        with self.assertRaises(ValueError): self.service.resend_notifications('one')
        self.data['runs'][0]['status'] = 'running'
        with self.assertRaises(ValueError): self.service.resend_notifications('one')
        dispatch.assert_not_called()

    def test_audit_does_not_change_sources(self):
        task = {'sources': ['https://news.google.com/rss/search?q=AI+自动执行网络搜索+site:example.com+when:1d']}
        original = deepcopy(task)
        self.assertEqual(len(inspect_search_task(task)), 1)
        self.assertEqual(task, original)
        self.assertEqual(inspect_search_task({'keywords': ['AI', '财税政策']}), [])

    def test_audit_unified_task_index(self):
        task = {'definition': {'conditions': {'keywords': ['自动执行网络搜索', 'AI']},
                'sources': [{'type': 'feed_or_url', 'value': 'https://news.google.com/rss/search?q=AI+通过可用搜索引擎'}]}}
        self.assertEqual(len(inspect_search_task(task)), 2)


if __name__ == '__main__': unittest.main()
