import io
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from plugins.intelligence_center.website_monitor import WebsiteMonitorService


class _Response(io.BytesIO):
    status=200
    def __init__(self,data):
        super().__init__(data.encode()); self.headers=Message(); self.headers['Content-Type']='text/html; charset=utf-8'
    def __enter__(self): return self
    def __exit__(self,*args): self.close()


class WebsiteMonitorTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('work/test-data')/uuid.uuid4().hex; self.root.mkdir(parents=True)
        self.service=WebsiteMonitorService(str(self.root))
    def tearDown(self):
        for path in sorted(self.root.rglob('*'),reverse=True): path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_first_run_baseline_then_detects_meaningful_change(self):
        task=self.service.save_task({'name':'竞品价格','urls':['https://example.com/pricing'],'keywords':['价格','API']})
        with patch('urllib.request.urlopen',return_value=_Response('<html><body><h1>价格</h1><p>专业版 99 元</p></body></html>')):
            first=self.service.run(task['id'])
        self.assertTrue(first['changes'][0]['baseline']); self.assertFalse(first['changes'][0]['changed'])
        with patch('urllib.request.urlopen',return_value=_Response('<html><body><h1>价格</h1><p>专业版 79 元</p><p>新增 API</p></body></html>')):
            second=self.service.run(task['id'])
        change=second['changes'][0]; self.assertTrue(change['changed']); self.assertIn('API',change['keyword_hits']); self.assertTrue(change['additions'])

    def test_scripts_and_styles_do_not_trigger_content(self):
        cleaned=self.service._clean_html('<style>.x{}</style><script>track()</script><main>有效内容</main>')
        self.assertEqual('有效内容',cleaned)

    def test_url_validation(self):
        with self.assertRaises(ValueError): self.service.save_task({'name':'错误','urls':['file:///etc/passwd']})

    def test_interval_waits_until_due(self):
        task=self.service.save_task({'name':'高频','urls':['https://example.com'],'schedule_type':'interval','interval_minutes':15})
        now=datetime(2026,8,25,10,0,tzinfo=timezone.utc); self.service._update(task['id'],{'last_run_at':(now-timedelta(minutes=14)).isoformat()})
        with patch.object(self.service,'run') as run: self.assertEqual([],self.service.run_due(now)); run.assert_not_called()
        self.service._update(task['id'],{'last_run_at':(now-timedelta(minutes=15)).isoformat()})
        with patch.object(self.service,'run',return_value={'ok':True}): self.assertEqual([{'ok':True}],self.service.run_due(now))

    def test_github_release_notes_are_summarized_in_chinese(self):
        notes = """# Hermes Agent v0.21.0 The Pantheon Release.
        Since v0.20.0: ~5,800 commits, ~2,475 merged PRs, ~6,580 files changed,
        2,100 issues closed. Bot Mode ships built into the desktop app with named
        agents, faces and group chats. Persistent memory and skills were improved."""
        summaries = self.service._chinese_release_summary(notes)
        text = "\n".join(summaries)
        self.assertIn("Bot 模式", text)
        self.assertIn("长期记忆", text)
        self.assertIn("5,800", text)
        self.assertNotIn("ships built into", text)


if __name__=='__main__': unittest.main()
