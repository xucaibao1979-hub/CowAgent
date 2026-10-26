import ast
import unittest
import uuid
import re
from collections import OrderedDict
from urllib.parse import quote, urlparse, unquote
from pathlib import Path
from unittest.mock import patch, Mock
from intelligence_center.sqlite_state import SQLiteStateStore


class MaintenanceCanonicalTests(unittest.TestCase):
    def method(self):
        path = Path('channel/web/web_channel.py')
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MaintenanceDashboardHandler')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_automation_runs')
        node.decorator_list = []
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), ns)
        return ns['_automation_runs']

    def test_reads_all_canonical_runs(self):
        runs = [{'id': str(i)} for i in range(250)]
        with patch.object(SQLiteStateStore, 'read', return_value={'runs': runs}) as read:
            self.assertEqual(self.method()('work/sample'), runs)
            self.assertEqual(read.call_args.args[0], 'automation_runs')

    def test_failure_is_not_reported_as_zero(self):
        with patch.object(SQLiteStateStore, 'read', side_effect=OSError('unavailable')):
            with self.assertRaises(OSError):
                self.method()('work/sample')

    def test_connections_are_closed(self):
        store = SQLiteStateStore('unused')
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = ('{}',)
        with patch.object(store, '_connect', return_value=connection):
            store.read('test', {})
            connection.close.assert_called_once()
        connection.reset_mock()
        with patch.object(store, '_connect', return_value=connection):
            store.write('test', {})
            connection.close.assert_called_once()


class RecoveryScopeTests(unittest.TestCase):
    def test_scheduled_recovery_obeys_same_scope(self):
        from plugins.intelligence_center.automation_runs import _scheduled_recovery_patch
        task = {'sources': ['https://news.google.com/rss/search?q=AI%20site%3Aopenai.com%20when%3A1d'], 'rules': {'include': ['MCP']}, 'max_items': 5}
        changes, actions = _scheduled_recovery_patch('news', task, lessons=[{'lesson': '空结果'}], policy={'enabled': True, 'allow_expand_time': False, 'allow_switch_sources': True})
        self.assertNotIn('rules', changes)
        self.assertNotIn('max_items', changes)
        self.assertTrue(all('site:openai.com' in unquote(x) and 'when:7d' not in unquote(x) for x in changes['sources']))

    def test_revision_is_stable_and_injected(self):
        from channel.web.page_revision import page_revision, stamp_page
        revision = page_revision()
        self.assertEqual(revision, page_revision())
        self.assertEqual(revision, stamp_page('{{COW_PAGE_REVISION}}'))

    def handler(self):
        path = Path('channel/web/web_channel.py')
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IntelligenceRunsHandler')
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {'_news_fallback_sources', '_recovery_retry_patch'}]
        ns = dict(re=re, OrderedDict=OrderedDict, quote=quote, urlparse=urlparse)
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), 'exec'), ns)
        return ns['IntelligenceRunsHandler']

    def test_retry_preserves_keywords_count_domain_and_time(self):
        snapshot = {'sources': ['https://news.google.com/rss/search?q=AI%20site%3Aopenai.com%20when%3A1d'],
                    'rules': {'include': ['MCP']}, 'max_items': 5}
        patch_data, actions = self.handler()._recovery_retry_patch('news',
            {'task_snapshot': snapshot, 'status': '暂无内容'},
            preferred=['搜索无内容：本次重试已放宽必须包含词'],
            policy={'allow_expand_time': False, 'allow_switch_sources': True})
        self.assertNotIn('rules', patch_data)
        self.assertNotIn('max_items', patch_data)
        self.assertTrue(all('site:openai.com' in unquote(x) for x in patch_data['sources']))
        self.assertTrue(all('when:7d' not in unquote(x) for x in patch_data['sources']))
        self.assertFalse(any('放宽必须包含' in x for x in actions))
