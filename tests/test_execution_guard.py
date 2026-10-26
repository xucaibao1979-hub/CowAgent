import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from plugins.intelligence_center.execution_guard import task_process_lock


class ExecutionGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path('work/test-data') / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)

    def test_unfinished_record_prevents_restart_replay(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from plugins.intelligence_center.automation_runs import AutomationRunService, tracked_run
        AutomationRunService(self.root).start({'id': 'one', 'name': '中断测试'}, 'news')
        service = SimpleNamespace(root=self.root)
        runner = Mock()
        with self.assertRaisesRegex(RuntimeError, '不会自动重复执行'):
            tracked_run(service, 'one', 'news', 'scheduled', runner)
        runner.assert_not_called()

    def child(self, task):
        code = "from plugins.intelligence_center.execution_guard import task_process_lock; import sys;\nwith task_process_lock(sys.argv[1],'news',sys.argv[2]): print('acquired')"
        return subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(self.root), task], capture_output=True, text=True, timeout=15)

    def test_other_process_rejected_and_different_task_allowed(self):
        with task_process_lock(self.root, 'news', 'one'):
            blocked = self.child('one')
            self.assertNotEqual(0, blocked.returncode)
            self.assertIn('未重复执行', blocked.stderr)
            self.assertEqual(0, self.child('two').returncode)
        self.assertEqual(0, self.child('one').returncode)

    def test_terminated_worker_releases_os_lock(self):
        code = "from plugins.intelligence_center.execution_guard import task_process_lock; import sys,time;\nwith task_process_lock(sys.argv[1],'news','one'):\n print('ready',flush=True)\n time.sleep(30)"
        worker = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', code, str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual('ready', worker.stdout.readline().strip())
            self.assertNotEqual(0, self.child('one').returncode)
            worker.terminate(); worker.wait(timeout=10)
            self.assertEqual(0, self.child('one').returncode)
        finally:
            if worker.poll() is None: worker.kill(); worker.wait(timeout=10)
            worker.stdout.close(); worker.stderr.close()
