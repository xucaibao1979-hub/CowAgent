import json
import uuid
import shutil
import gc
import unittest
from pathlib import Path

from intelligence_center.harness_evolution_service import HarnessEvolutionService
from intelligence_center.sqlite_state import SQLiteStateStore


class HarnessEvolutionTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).resolve().parents[1] / 'work' / 'harness-tests'
        self.root = test_root / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.root)
        self.addCleanup(gc.collect)
        self.service = HarnessEvolutionService(self.root)

    def write(self, name, data):
        (self.root/name).write_text(json.dumps(data), encoding='utf-8')

    def test_trace_uses_database_and_real_delivery_result(self):
        self.write('automation_runs.json', {'runs': [{'id': 'stale'}]})
        SQLiteStateStore(self.root).write('automation_runs', {'runs': [{
            'id': 'current', 'status': 'success',
            'notification_results': [{'channel': 'weixin', 'success': False}],
            'report_path': 'missing.md'}]})
        trace = self.service.traces()[0]
        self.assertEqual(trace['id'], 'current')
        self.assertFalse(trace['notification_written'])
        self.assertFalse(trace['weixin_written'])
        self.assertIsNone(trace['artifact_written'])

    def test_rollback_preserves_runtime_history_and_backups(self):
        self.write('agent2_state.json', {'settings': {'heartbeat_enabled': True}, 'events': [1]})
        snapshot = self.service.create_snapshot()
        self.write('agent2_state.json', {'settings': {'heartbeat_enabled': False}, 'events': [1, 2]})
        result = self.service.rollback_snapshot(snapshot['id'])
        current = json.loads((self.root/'agent2_state.json').read_text())
        self.assertTrue(current['settings']['heartbeat_enabled'])
        self.assertEqual(current['events'], [1, 2])
        self.assertNotEqual(result['backup']['id'], snapshot['id'])

    def test_corruption_and_traversal_rejected(self):
        self.write('personal_agent_profile.json', {'role': 'developer'})
        snapshot = self.service.create_snapshot()
        (self.service.snapshot_dir/snapshot['id']/'personal_agent_profile.json').write_text('{}')
        with self.assertRaises(ValueError):
            self.service.rollback_snapshot(snapshot['id'])
        with self.assertRaises(ValueError):
            self.service.rollback_snapshot('../outside')
        self.assertEqual(json.loads((self.root/'personal_agent_profile.json').read_text())['role'], 'developer')

    def test_daily_review_registers_one_report(self):
        first = self.service.review()
        second = self.service.review()
        self.assertEqual(first['review']['id'], second['review']['id'])
        self.assertTrue(first['review']['artifact_id'])
        self.assertEqual(len(json.loads((self.root/'artifacts.json').read_text(encoding='utf-8'))['artifacts']), 1)

    def test_approved_patch_is_guarded_verified_and_idempotent(self):
        from intelligence_center.agent2_service import Agent2Service
        service = Agent2Service(self.root, self.root, {})
        data = service._default()
        data['settings']['recovery_policy']['allow_web_fallback'] = False
        self.write('agent2_state.json', data)
        preview = {'suggestion_id': 'notification-fallback',
                   'baseline': self.service._digest(data['settings']['recovery_policy'])}
        with self.assertRaises(ValueError):
            self.service.apply_approved_patch(preview, {})
        approval = {'id': 'test-approval', 'action': 'harness_policy_patch', 'approval_granted_at': '2026-09-10'}
        result = self.service.apply_approved_patch(preview, approval)
        self.assertTrue(result['verified'])
        self.assertTrue(service._read()['settings']['recovery_policy']['allow_web_fallback'])
        self.assertEqual(self.service.apply_approved_patch(preview, approval), result)
        approval['id'] = 'new-approval'
        with self.assertRaises(ValueError):
            self.service.apply_approved_patch(preview, approval)

    def test_trace_includes_unlinked_tools_without_claiming_final_success(self):
        logs = self.root / 'logs'
        logs.mkdir()
        (logs/'tool_audit.jsonl').write_text(json.dumps({'run_id':'chat-run', 'session_id':'session-a',
            'tool':'web_search','success':True,'status':'success','timestamp':'2026-09-10T10:00:00+08:00'})+'\n', encoding='utf-8')
        trace = self.service.traces()[0]
        self.assertEqual(trace['id'], 'chat-run')
        self.assertEqual(trace['session_id'], 'session-a')
        self.assertEqual(trace['status'], 'observed')
        self.assertIsNone(trace['notification_written'])
        self.assertEqual(trace['stages'][0]['name'], 'web_search')

    def test_task_rule_versions_restore_only_selected_configuration(self):
        from plugins.intelligence_center.hub import AutomationHubService
        from intelligence_center.task_rule_versions import TaskRuleVersions
        self.write('news_tasks.json', {'tasks':[
            {'id':'a','name':'old','sources':['https://example.com/rss'],'last_run_at':'keep'},
            {'id':'b','name':'other','sources':[]} ]})
        hub = AutomationHubService(self.root)
        versions = TaskRuleVersions(self.root)
        hub.update_task('news','a', {'name':'new','rules':{'instruction':'short summary'}})
        history = versions.list()
        self.assertEqual(len(history), 1)
        hub.update_task('news','a', {'name':'new'})
        self.assertEqual(len(versions.list()), 1)
        result = versions.rollback(history[0]['id'])
        self.assertTrue(result['backup_id'])
        tasks = json.loads((self.root/'news_tasks.json').read_text(encoding='utf-8'))['tasks']
        self.assertEqual(tasks[0]['name'], 'old')
        self.assertEqual(tasks[0]['last_run_at'], 'keep')
        self.assertEqual(tasks[1]['name'], 'other')
        self.assertNotIn('rules', tasks[0])
        self.assertEqual(tasks[0]['definition']['processor']['instruction'], '')

    def test_task_version_refuses_corruption_and_deleted_task(self):
        from intelligence_center.task_rule_versions import TaskRuleVersions
        versions = TaskRuleVersions(self.root)
        item = versions.record('news', {'id':'a','name':'old'}, {'id':'a','name':'new'})
        self.write('news_tasks.json', {'tasks':[]})
        with self.assertRaises(ValueError):
            versions.rollback(item['id'])
        with self.assertRaises(ValueError):
            versions.rollback('../escape')
        path = versions.folder/(item['id']+'.json')
        envelope = json.loads(path.read_text(encoding='utf-8'))
        envelope['data']['before']['name'] = 'tampered'
        path.write_text(json.dumps(envelope), encoding='utf-8')
        with self.assertRaises(ValueError):
            versions.rollback(item['id'])


    def test_scheduler_version_restores_content_preserves_execution(self):
        from intelligence_center.task_rule_versions import TaskRuleVersions
        from agent.tools.scheduler.task_store import TaskStore
        versions = TaskRuleVersions(self.root)
        store = TaskStore(str(self.root / 'scheduler' / 'tasks.json'))
        before = {'id': 'cron1', 'name': '早报', 'enabled': True,
                  'schedule': {'type': 'interval', 'seconds': 3600},
                  'action': {'type': 'agent_task', 'task_description': '旧内容', 'receiver': 'web'}}
        after = dict(before, action=dict(before['action'], task_description='新内容'), last_run_status='success')
        store.add_task(after)
        version = versions.record('scheduler', before, after, workspace=self.root)
        self.assertEqual(version['changed_fields'], ['action'])
        self.assertIsNone(versions.record('scheduler', after, dict(after, last_run_status='failed'), workspace=self.root))
        with self.assertRaises(ValueError):
            versions.rollback_scheduler(version['id'], self.root / 'other')
        versions.rollback_scheduler(version['id'], self.root)
        result = store.get_task('cron1')
        self.assertEqual(result['action']['task_description'], '旧内容')
        self.assertEqual(result['last_run_status'], 'success')
        self.assertTrue(result['next_run_at'])
        self.assertEqual(len(versions.list()), 2)
        store.delete_task('cron1')
        with self.assertRaises(ValueError):
            versions.rollback_scheduler(version['id'], self.root)

    def test_message_trace_correlates_tools_without_claiming_delivery(self):
        from unittest.mock import patch
        from common.task_trace import traced, emit, read_events
        from common.runtime_identity import current_identity
        from bridge.reply import Reply, ReplyType
        data_root = self.root / 'data'
        trace_root = data_root / 'intelligence_center'
        context = {'session_id': 'weekly-session', 'channel_type': 'weixin', 'request_id': 'message-1'}
        @traced('生成回复', 0)
        def execute(ctx):
            self.assertEqual(current_identity().run_id, ctx['trace_id'])
            return Reply(ReplyType.TEXT, 'secret reply must not be logged')
        with patch('config.get_data_root', return_value=str(data_root)):
            execute(context)
            emit(context, '发送回复', 'failed', notification_written=False)
        service = HarnessEvolutionService(trace_root)
        with patch.object(service, '_tool_events', return_value=[{'run_id':'tool-child', 'parent_run_id':context['trace_id'], 'tool':'mail', 'success':True}]):
            traces = service.traces()
        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0]['session_id'], 'weekly-session')
        self.assertEqual(traces[0]['tools'], ['mail'])
        self.assertFalse(traces[0]['notification_written'])
        self.assertNotIn('secret reply', json.dumps(read_events(trace_root)))
        self.assertIsNone(current_identity().run_id)

    def test_trace_exception_is_recorded_and_context_restored(self):
        from unittest.mock import patch
        from common.task_trace import traced, read_events
        from common.runtime_identity import current_identity
        @traced('生成回复', 0)
        def fail(context):
            raise RuntimeError('password=must-not-be-recorded')
        with patch('config.get_data_root', return_value=str(self.root)):
            with self.assertRaises(RuntimeError):
                fail({'channel':'remote_bridge', 'session_id':'remote:1'})
        events = read_events(self.root / 'intelligence_center')
        self.assertEqual(events[-1]['status'], 'failed')
        self.assertNotIn('password', json.dumps(events))
        self.assertIsNone(current_identity().run_id)

    def test_remote_receipt_requires_owner_and_completed_result(self):
        from common.task_trace import confirm_remote_result, emit_remote_receipt
        from unittest.mock import patch
        item = {'id':'command-1', 'trace_id':'attempt-1', 'source_device_id':'phone-1', 'status':'running'}
        with self.assertRaises(ValueError):
            confirm_remote_result(item, 'phone-1')
        item['status'] = 'done'
        with self.assertRaises(ValueError):
            confirm_remote_result(item, 'phone-2')
        with self.assertRaises(ValueError):
            confirm_remote_result(item, 'phone-1', 'failed')
        confirm_remote_result(item, 'phone-1')
        with patch('config.get_data_root', return_value=str(self.root)):
            emit_remote_receipt(item)
        result = HarnessEvolutionService(self.root / 'intelligence_center').traces()[0]
        self.assertTrue(result['device_received'])
        self.assertEqual(result['id'], 'attempt-1')
        self.assertEqual(result['source_id'], 'command-1')

    def test_weixin_shortcut_shares_trace_without_storing_secrets(self):
        from common.task_trace import weixin_entry, send_observer, read_events
        from unittest.mock import patch
        from common.runtime_identity import current_identity
        class FakeChannel:
            def _conversation_session_id(self, user):
                return 'week-session'
            @send_observer
            def send(self):
                return False
            @weixin_entry
            def process(self, raw, user, token):
                self.run_id = current_identity().run_id
                return self.send()
        channel = FakeChannel()
        with patch('config.get_data_root', return_value=str(self.root)):
            self.assertFalse(channel.process({'message_id':'wx1'}, 'private-user', 'secret-cookie'))
        events = read_events(self.root / 'intelligence_center')
        self.assertEqual({x['id'] for x in events}, {channel.run_id})
        self.assertEqual(events[0]['session_id'], 'week-session')
        self.assertFalse(events[-1]['notification_written'])
        self.assertNotIn('secret-cookie', json.dumps(events))
        self.assertIsNone(current_identity().run_id)

if __name__ == '__main__':
    unittest.main()
