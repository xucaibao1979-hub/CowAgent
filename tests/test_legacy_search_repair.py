import unittest
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
from scripts.repair_legacy_search_20260910 import repair_patch


class LegacySearchRepairTests(unittest.TestCase):
    def test_scope_and_time_preserved(self):
        task = {'id': '59337ae0fad2', 'keywords': ['自动执行网络搜索', 'AI'],
                'sources': ['https://news.google.com/rss/search?q=AI+自动执行网络搜索+site:36kr.com+when:1d&hl=zh-CN'],
                'rules': {'include': ['AI'], 'exclude': ['无'], 'instruction': '摘要'},
                'enabled': False, 'notifications': [{'type': 'web'}]}
        before = deepcopy(task)
        patch = repair_patch(task)
        self.assertEqual(task, before)
        self.assertEqual(set(patch), {'keywords', 'sources', 'rules'})
        query = parse_qs(urlsplit(patch['sources'][0]).query)
        self.assertEqual(query['q'], ['AI IT site:36kr.com when:1d'])
        self.assertEqual(query['hl'], ['zh-CN'])
        self.assertEqual(patch['rules'], {'include': ['AI'], 'exclude': [], 'instruction': '摘要'})
        self.assertEqual(repair_patch(dict(task, **patch)), {})

    def test_domestic_sources_untouched(self):
        task = {'id': 'f84aac62811c', 'keywords': ['自动执行网络搜索', '国内热点'],
                'sources': ['https://example.org/feed'], 'rules': {'include': ['无强制词'], 'exclude': ['招聘']}}
        patch = repair_patch(task)
        self.assertNotIn('sources', patch)
        self.assertEqual(patch['rules']['exclude'], ['招聘'])

    def test_unexpected_source_refused(self):
        with self.assertRaises(ValueError):
            repair_patch({'id': '59337ae0fad2', 'sources': ['https://example.org/?q=AI']})
