import io
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from plugins.intelligence_center.github_monitor import GitHubMonitorService


class _Response(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def _github_response(request, timeout=20):
    url = request.full_url
    if "/releases" in url: data = [{"tag_name": "v2.0.0"}]
    elif "/issues" in url: data = [{"number": 1}, {"number": 2, "pull_request": {}}]
    elif "/commits" in url: data = [{"sha": "a"}, {"sha": "b"}]
    else: data = {"html_url": "https://github.com/openai/openai-python", "description": "Official SDK", "stargazers_count": 25000, "forks_count": 4000, "updated_at": "2026-08-25T08:00:00Z"}
    return _Response(json.dumps(data).encode())


class GitHubMonitorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("work/test-data") / uuid.uuid4().hex; self.root.mkdir(parents=True)
        self.service = GitHubMonitorService(str(self.root), "test-token")

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True): path.unlink() if path.is_file() else path.rmdir()
        self.root.rmdir()

    def test_repository_normalization_and_validation(self):
        self.assertEqual("openai/openai-python", self.service._normalize_repo("https://github.com/openai/openai-python.git"))
        with self.assertRaises(ValueError): self.service._normalize_repo("https://example.com/a/b")

    def test_monitor_collects_release_issue_star_and_commits(self):
        task = self.service.save_task({"name":"SDK监控", "repositories":["openai/openai-python"]})
        with patch("urllib.request.urlopen", side_effect=_github_response): result = self.service.run(task["id"])
        project = result["projects"][0]
        self.assertEqual("v2.0.0", project["latest_release"])
        self.assertEqual(1, project["open_issues"])
        self.assertEqual(25000, project["stars"])
        self.assertEqual(2, project["recent_commits"])
        self.assertTrue(Path(result["report_path"]).exists())

    def test_interval_schedule_waits_until_due(self):
        task = self.service.save_task({"name":"高频", "repositories":["openai/openai-python"], "schedule_type":"interval", "interval_minutes":60})
        now = datetime(2026,8,25,10,0,tzinfo=timezone.utc)
        self.service._update(task["id"], {"last_run_at":(now-timedelta(minutes=59)).isoformat()})
        with patch.object(self.service,"run") as run: self.assertEqual([], self.service.run_due(now)); run.assert_not_called()
        self.service._update(task["id"], {"last_run_at":(now-timedelta(minutes=60)).isoformat()})
        with patch.object(self.service,"run",return_value={"ok":True}) as run: self.assertEqual([{"ok":True}],self.service.run_due(now))

    def test_toggle_and_delete(self):
        task=self.service.save_task({"name":"项目", "repositories":["owner/repo"]})
        self.assertFalse(self.service.toggle(task["id"],False)["enabled"])
        self.service.delete(task["id"]); self.assertEqual([],self.service.list_tasks())


if __name__ == "__main__": unittest.main()
