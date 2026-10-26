import io
import unittest
import uuid
from email.message import Message
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.intelligence_center.news import FISCAL_OFFICIAL_SOURCES, NewsAssistantService


FEED = b'''<?xml version="1.0"?><rss><channel>
<item><title>AI Agent product launch</title><link>https://example.com/a</link><description>New MCP tools.</description></item>
<item><title>AI Agent product launch</title><link>https://example.com/a2</link><description>Duplicate.</description></item>
<item><title>Sports result</title><link>https://example.com/b</link><description>Unrelated.</description></item>
</channel></rss>'''


class _Response(io.BytesIO):
    def __init__(self, body):
        super().__init__(body)
        self.headers = Message()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class NewsAssistantTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("work/test-data") / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.service = NewsAssistantService(str(self.root))
        network = patch('plugins.intelligence_center.news.safe_urlopen', side_effect=lambda *a, **k: _Response(FEED))
        network.start()
        self.addCleanup(network.stop)
        personal = patch('plugins.intelligence_center.news.load_personal_context', return_value={})
        personal.start()
        self.addCleanup(personal.stop)

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_create_fetch_filter_deduplicate_and_report(self):
        task = self.service.save_task({"name": "AI日报", "sources": ["https://example.com/feed"], "keywords": ["MCP"], "schedule": "每天08:00"})
        with patch("urllib.request.urlopen", return_value=_Response(FEED)):
            result = self.service.run(task["id"])
        self.assertEqual(1, len(result["items"]))
        self.assertIn("栏目一｜隔夜重点", result["report"])
        self.assertTrue(Path(result["report_path"]).exists())

    def test_isolated_create_execute_artifact_notification_and_duplicate(self):
        from plugins.intelligence_center.automation_runs import AutomationRunService
        task = self.service.save_task({'name': '隔离闭环验收', 'sources': ['https://example.com/feed'],
            'keywords': ['MCP'], 'notifications': [{'type': 'channel', 'channel': 'web', 'receiver': 'acceptance-only'}]})
        with patch('plugins.intelligence_center.notifications.NotificationDispatcher.dispatch',
                   return_value=[{'target': 'web', 'channel': 'web', 'receiver': 'acceptance-only', 'success': True}]) as send:
            result = self.service.run(task['id'])
            duplicate = self.service.run(task['id'])
        send.assert_called_once()
        self.assertEqual(result['report_path'], duplicate['report_path'])
        runs = AutomationRunService(self.root).list()['items']
        self.assertEqual(1, len(runs))
        self.assertEqual('success', runs[0]['status'])
        self.assertTrue(runs[0]['artifact'].get('id'))
        self.assertEqual(runs[0]['id'], runs[0]['artifact'].get('run_id'))
        self.assertTrue(runs[0]['notification_results'][0]['success'])

    def test_due_task_runs_once_per_day(self):
        task = self.service.save_task({"name": "早报", "sources": ["https://example.com/feed"], "keywords": [], "schedule": "每天08:00"})
        with patch("urllib.request.urlopen", return_value=_Response(FEED)):
            now = datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)
            self.assertEqual(1, len(self.service.run_due(now)))
            self.assertEqual(0, len(self.service.run_due(now + timedelta(hours=1))))

    def test_toggle_and_delete(self):
        task = self.service.save_task({"name": "日报", "sources": ["https://example.com/feed"], "keywords": []})
        self.assertFalse(self.service.toggle(task["id"], False)["enabled"])
        self.service.delete(task["id"])
        self.assertEqual([], self.service.list_tasks())

    def test_empty_mandatory_keywords_are_not_silently_relaxed(self):
        task = self.service.save_task({'name': '严格筛选', 'sources': ['https://example.com/feed'],
            'keywords': [], 'rules': {'include': ['NONEXISTENT'], 'exclude': []}})
        result = self.service._execute(task['id'])
        self.assertEqual([], result['items'])

    def test_named_briefing_respects_requested_count(self):
        task = self.service.save_task({'name': 'IT早报', 'sources': ['https://example.com/feed'],
                                      'keywords': [], 'max_items': 1})
        result = self.service._execute(task['id'])
        self.assertEqual(1, len(result['items']))

    def test_interval_task_is_optional_and_runs_when_due(self):
        task = self.service.save_task({
            "name": "高频监控", "sources": ["https://example.com/feed"], "keywords": [],
            "schedule_type": "interval", "interval_minutes": 30,
        })
        now = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        self.service._update(task["id"], {"last_run_at": (now - timedelta(minutes=29)).isoformat()})
        with patch.object(self.service, "run") as run_mock:
            self.assertEqual([], self.service.run_due(now))
            run_mock.assert_not_called()
        self.service._update(task["id"], {"last_run_at": (now - timedelta(minutes=30)).isoformat()})
        with patch.object(self.service, "run", return_value={"ok": True}) as run_mock:
            self.assertEqual([{"ok": True}], self.service.run_due(now))
            run_mock.assert_called_once_with(task["id"], 'scheduled')

    def test_interval_validation(self):
        with self.assertRaises(ValueError):
            self.service.save_task({"name": "错误间隔", "sources": ["https://example.com/feed"], "schedule_type": "interval", "interval_minutes": 0})

    def test_report_uses_short_title_summary_and_inline_link(self):
        item = {
            "title": "日本财政大臣表示将继续维护全球金融市场稳定 - cj.sina.cn",
            "summary": "日本财政大臣表示将继续维护全球金融市场稳定 - cj.sina.cn",
            "link": "https://example.com/fiscal",
        }
        report = self.service._build_report({"name": "财税日报"}, [item], [], "今日摘要")
        self.assertIn("1. 日本财政大臣表示将继续维护全球金融市场稳定 [链接](https://example.com/fiscal)", report)
        self.assertNotIn("\n[查看原文]", report)
        summary = report.split("[链接](https://example.com/fiscal)\n", 1)[1].splitlines()[0].strip()
        self.assertGreaterEqual(len(summary.rstrip("。")), 30)
        self.assertLessEqual(len(summary.rstrip("。")), 50)

    def test_fiscal_task_is_detected_and_has_official_sources(self):
        task = {"name": "最新财税政策", "keywords": ["税务"], "rules": {"include": ["财政"]}}
        self.assertTrue(self.service._is_fiscal_task(task))
        self.assertTrue(any("mof.gov.cn" in source for source in FISCAL_OFFICIAL_SOURCES))
        self.assertTrue(any("chinatax.gov.cn" in source for source in FISCAL_OFFICIAL_SOURCES))

    def test_html_policy_listing_is_parsed_without_javascript(self):
        page = '''<!doctype html><html><body><ul>
        <li><a href="/policy/a.html">关于进一步支持小微企业发展的税收政策公告</a><span>2026-09-01</span></li>
        <li><a href="javascript:void(0)">查看更多</a></li>
        </ul></body></html>'''.encode("utf-8")
        items = self.service._fetch_html_listing("https://example.gov.cn/list/", page, "utf-8")
        self.assertEqual(1, len(items))
        self.assertEqual("2026-09-01", items[0]["published_at"])
        self.assertEqual("https://example.gov.cn/policy/a.html", items[0]["link"])

    def test_source_health_records_success_and_failure(self):
        source = "https://example.gov.cn/policy/"
        self.service._record_source_outcome(source, "error", 0, "timeout")
        self.service._record_source_outcome(source, "success", 8)
        health = self.service._load_source_health()[source]
        self.assertEqual(1, health["failures"])
        self.assertEqual(1, health["successes"])
        self.assertEqual(8, health["items_collected"])
        self.assertEqual(0, health["consecutive_failures"])


if __name__ == "__main__":
    unittest.main()
