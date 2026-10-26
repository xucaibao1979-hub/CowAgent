import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from plugins.intelligence_center.extended_tasks import ExtendedTaskService


class Response:
    status = 200
    def __init__(self, body): self.body = body; self.headers = self
    def read(self): return self.body
    def __enter__(self): return self
    def __exit__(self, *args): pass


class ExtendedTaskTests(unittest.TestCase):
    def setUp(self):
        self.root=Path('work/test-data')/uuid.uuid4().hex; self.root.mkdir(parents=True); self.service = ExtendedTaskService(str(self.root))
    def tearDown(self):
        for path in sorted(self.root.rglob('*'),reverse=True): path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_social_feed_lifecycle_and_report(self):
        task = self.service.save_task({"kind":"social","name":"热点","sources":["https://example.com/feed"],"keywords":["AI"]})
        feed = b"<rss><channel><item><title>AI Agent trend</title><link>https://example.com/a</link><description>fast growth</description></item></channel></rss>"
        with patch("urllib.request.urlopen", return_value=Response(feed)):
            result = self.service.run(task["id"])
        self.assertEqual(len(result["results"]), 1); self.assertIn("社交热点监控", result["report"])
        self.assertTrue(Path(result["report_path"]).exists())
        self.service.toggle(task["id"], False); self.assertFalse(self.service.list_tasks()[0]["enabled"])
        self.service.delete(task["id"]); self.assertEqual(self.service.list_tasks(), [])

    def test_ops_http_report(self):
        task = self.service.save_task({"kind":"ops","name":"运维","sources":["http://localhost/health"]})
        with patch("urllib.request.urlopen", return_value=Response(b"ok")):
            result = self.service.run(task["id"])
        self.assertTrue(result["results"][0]["healthy"]); self.assertIn("HTTP 200", result["report"])


if __name__ == "__main__": unittest.main()
