"""Isolated lifecycle acceptance; external transports never run."""
import json
import subprocess
import sys
import uuid
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

from plugins.intelligence_center.automation_runs import AutomationRunService
from plugins.intelligence_center.website_monitor import WebsiteMonitorService
from plugins.intelligence_center.email_secretary import EmailSecretaryService
from tests.test_email_secretary import FakeImap


class DeliveryAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path('work/test-data') / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.runs = AutomationRunService(self.root)
        self.target = {'type': 'channel', 'channel': 'web', 'receiver': 'acceptance-only'}
        self.sent = {'target': 'web', 'channel': 'web', 'receiver': 'acceptance-only', 'success': True}
        for name in ('socket.create_connection', 'socket.socket.connect'):
            blocker = patch(name, side_effect=AssertionError('Undeclared network access'))
            blocker.start(); self.addCleanup(blocker.stop)
        sender = patch('plugins.intelligence_center.notifications.NotificationDispatcher.dispatch', return_value=[self.sent])
        self.send = sender.start(); self.addCleanup(sender.stop)

    def assert_delivery(self, result):
        row = self.runs.get(result['run_id'])
        self.assertTrue(Path(row['report_path']).is_file())
        self.assertTrue(row['artifact'].get('id'))
        self.assertEqual(row['id'], row['artifact'].get('run_id'))
        self.assertEqual('success', row['status'])
        self.send.assert_called_once()

    def test_email_delivery(self):
        service = EmailSecretaryService(str(self.root), {'imap_host': 'mail.example.com', 'username': 'test', 'password': 'test'})
        task = service.save_task({'name': '邮件验收', 'notifications': [self.target]})
        with patch('imaplib.IMAP4_SSL', FakeImap):
            result = service.run(task['id'])
        self.assert_delivery(result)

    def test_forex_delivery_and_fresh_process_does_not_repeat_today(self):
        from plugins.intelligence_center.forex_query import ForexQueryService
        service = ForexQueryService(str(self.root))
        task = service.save_task({'name': '外汇验收', 'schedule': '每天00:00', 'currencies': ['USD'], 'notifications': [self.target]})
        sample = {'rates': [{'code': 'USD', 'currency': '美元(USD)', 'buy': '700', 'sell': '701', 'published_at': '2026-09-10 09:00:00'}],
                  'queried_at': '2026-09-10T09:00:00+08:00', 'source_url': 'https://example.com/rates'}
        with patch.object(service, 'query_once', return_value=sample):
            result = service.run(task['id'])
        self.assert_delivery(result)
        code = "from plugins.intelligence_center.forex_query import ForexQueryService; import sys; from unittest.mock import patch; s=ForexQueryService(sys.argv[1]); p=patch.object(s,'run',side_effect=AssertionError('duplicate scheduled run')); p.start(); assert s.run_due()==[]; print('no-repeat')"
        output = subprocess.check_output([sys.executable, '-X', 'utf8', '-c', code, str(self.root)], text=True)
        self.assertIn('no-repeat', output)

    def test_briefing_delivery(self):
        from plugins.intelligence_center.daily_briefing import DailyBriefingService
        service = DailyBriefingService(str(self.root))
        service.reports.mkdir(parents=True)
        (service.reports / 'github-acceptance.md').write_text('# 测试报告\n隔离版本更新', encoding='utf-8')
        task = service.save_task({'name': '日报验收', 'include_types': ['github'], 'notifications': [self.target]})
        result = service.run(task['id'])
        self.assert_delivery(result)

    def test_website_unchanged_stays_quiet_but_keeps_report(self):
        service = WebsiteMonitorService(str(self.root))
        task = service.save_task({'name': '静默监控', 'urls': ['https://example.com'], 'notifications': [self.target], 'notify_unchanged': False})
        change = {'url': 'https://example.com', 'changed': False, 'baseline': True, 'keyword_hits': [], 'additions': [], 'removals': []}
        with patch.object(service, '_check_url', return_value=change):
            result = service.run(task['id'])
        self.send.assert_not_called()
        self.assertTrue(Path(result['report_path']).exists())
        self.assertTrue(self.runs.get(result['run_id'])['artifact'].get('id'))

    def test_restart_preserves_failed_send_and_resends_original_only(self):
        report = self.root / 'saved.md'
        report.write_text('Original acceptance report', encoding='utf-8')
        self.runs._write({'runs': [{'id': 'restart-case', 'status': 'success', 'report_path': str(report),
            'task_snapshot': {'notifications': [self.target]},
            'notification_results': [{**self.sent, 'success': False}]}]})
        code = "from plugins.intelligence_center.automation_runs import AutomationRunService; import sys,json; s=AutomationRunService(sys.argv[1]); print(json.dumps(s.get('restart-case')['notification_results'][0]['success']))"
        output = subprocess.check_output([sys.executable, '-X', 'utf8', '-c', code, str(self.root)], text=True)
        self.assertFalse(json.loads(output))
        restored = AutomationRunService(self.root)
        self.assertEqual(1, restored.resend_notifications('restart-case')['resent'])
        self.assertEqual('Original acceptance report', self.send.call_args.args[1])
        self.assertEqual(0, AutomationRunService(self.root).resend_notifications('restart-case')['resent'])
        self.send.assert_called_once()

    def test_restart_unknown_send_requires_confirmation(self):
        self.runs._write({'runs': [{'id': 'uncertain', 'status': 'success', 'notification_retry_state': 'sending'}]})
        with self.assertRaisesRegex(ValueError, '尚未确认'):
            AutomationRunService(self.root).resend_notifications('uncertain')
        self.send.assert_not_called()
