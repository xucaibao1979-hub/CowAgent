import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.intelligence_center.daily_briefing import DailyBriefingService


class DailyBriefingTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('work/test-data')/uuid.uuid4().hex; self.root.mkdir(parents=True)
        self.service=DailyBriefingService(str(self.root)); self.service.reports.mkdir(parents=True)
    def tearDown(self):
        for path in sorted(self.root.rglob('*'),reverse=True): path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_combines_news_github_and_website_reports(self):
        (self.service.reports/'abcdef123456-20260825-080000.md').write_text('# 新闻日报\nAI 新闻',encoding='utf-8')
        (self.service.reports/'github-task-20260825-090000.md').write_text('# GitHub\nRelease v2',encoding='utf-8')
        (self.service.reports/'website-task-20260825-100000.md').write_text('# 网站\n价格变化',encoding='utf-8')
        task=self.service.save_task({'name':'综合日报','include_types':['news','github','website'],'lookback_hours':24})
        result=self.service.run(task['id'])
        self.assertIn('新闻资讯',result['report']); self.assertIn('Release v2',result['report']); self.assertIn('价格变化',result['report'])
        self.assertEqual(3,sum(len(x['reports']) for x in result['sections']))

    def test_source_and_lookback_validation(self):
        with self.assertRaises(ValueError): self.service.save_task({'name':'空来源','include_types':[]})
        with self.assertRaises(ValueError): self.service.save_task({'name':'范围错误','include_types':['news'],'lookback_hours':1000})

    def test_interval_waits_until_due(self):
        task=self.service.save_task({'name':'综合','include_types':['news'],'schedule_type':'interval','interval_minutes':60})
        now=datetime(2026,8,25,10,0,tzinfo=timezone.utc); self.service._update(task['id'],{'last_run_at':(now-timedelta(minutes=59)).isoformat()})
        with patch.object(self.service,'run') as run: self.assertEqual([],self.service.run_due(now)); run.assert_not_called()
        self.service._update(task['id'],{'last_run_at':(now-timedelta(minutes=60)).isoformat()})
        with patch.object(self.service,'run',return_value={'ok':True}): self.assertEqual([{'ok':True}],self.service.run_due(now))

    def test_toggle_and_delete(self):
        task=self.service.save_task({'name':'综合','include_types':['news']}); self.assertFalse(self.service.toggle(task['id'],False)['enabled'])
        self.service.delete(task['id']); self.assertEqual([],self.service.list_tasks())


if __name__=='__main__': unittest.main()
