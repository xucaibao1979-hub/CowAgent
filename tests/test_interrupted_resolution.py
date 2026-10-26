import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from plugins.intelligence_center.automation_runs import AutomationRunService, tracked_run
from plugins.intelligence_center.execution_guard import task_process_lock


class InterruptedResolutionTests(unittest.TestCase):
    def setUp(self):
        self.root = (Path('work/test-data') / uuid.uuid4().hex).resolve()
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.store = AutomationRunService(self.root)
        self.run = self.store.start({'id':'one', 'name':'隔离中断'}, 'news')

    def test_confirmation_required(self):
        with self.assertRaises(ValueError): self.store.resolve_interrupted(self.run['id'])
        self.assertEqual('running', self.store.get(self.run['id'])['status'])

    def test_active_worker_cannot_be_closed(self):
        with task_process_lock(self.root, 'news', 'one'):
            with self.assertRaises(RuntimeError): self.store.resolve_interrupted(self.run['id'], True, '已检查')
        self.assertEqual('running', self.store.get(self.run['id'])['status'])

    def test_close_is_audited_and_does_not_schedule_replay(self):
        self.store.resolve_interrupted(self.run['id'], True, '报告未完成，通知状态需核对')
        row = AutomationRunService(self.root).get(self.run['id'])
        self.assertEqual('failed', row['status'])
        self.assertTrue(row['restart_hold'])
        self.assertEqual('close_without_replay', row['interruption_resolution']['action'])
        runner = Mock()
        with self.assertRaisesRegex(RuntimeError, '定时重跑暂缓'):
            tracked_run(SimpleNamespace(root=self.root), 'one', 'news', 'scheduled', runner)
        runner.assert_not_called()
        with self.assertRaises(ValueError): self.store.resolve_interrupted(self.run['id'], True, '重复提交')
