"""Per-task configuration history. Execution state is deliberately excluded."""
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

LOCK = threading.RLock()
FIELDS = set('name schedule schedule_type interval_minutes enabled rules notifications keywords model approval notify_unchanged artifact_type artifact_tags sources max_items repositories urls include_types lookback_hours kind folder unread_only max_messages recipient send_report currencies maintenance_type'.split())
SCHEDULER_FIELDS = {'name', 'schedule', 'enabled', 'action'}


def serialized_task_config(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with LOCK:
            return func(*args, **kwargs)
    return wrapped


class TaskRuleVersions:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.folder = self.root / 'task_rule_versions'

    @staticmethod
    def config(task):
        return {k: v for k, v in task.items() if k in FIELDS}

    @staticmethod
    def digest(data):
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def write(path, data):
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    @serialized_task_config
    def record(self, kind, before, after, reason='修改前备份', workspace=None):
        fields = SCHEDULER_FIELDS if kind == 'scheduler' else FIELDS
        old = {k: v for k, v in before.items() if k in fields}
        new = {k: v for k, v in after.items() if k in fields}
        if old == new:
            return None
        item = {'id': uuid.uuid4().hex, 'kind': kind, 'task_id': str(before['id']),
                'task_name': before.get('name', ''), 'before': old, 'after': new,
                'created_at': datetime.now().astimezone().isoformat(timespec='microseconds'),
                'reason': reason, 'changed_fields': sorted(k for k in old.keys() | new.keys()
                                                         if (k in old) != (k in new) or old.get(k) != new.get(k))}
        if kind == 'scheduler':
            if workspace is None:
                raise ValueError('定时任务版本必须关联工作空间')
            item['workspace_key'] = self.digest(os.path.normcase(str(Path(workspace).resolve())))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.write(self.folder / (item['id'] + '.json'), {'data': item, 'sha256': self.digest(item)})
        return item

    @serialized_task_config
    def rollback_scheduler(self, version_id, workspace):
        from agent.tools.scheduler.task_store import TaskStore
        from agent.tools.scheduler.scheduler_service import SchedulerService
        item = self.read(version_id)
        if item['kind'] != 'scheduler' or not set(item['before']).issubset(SCHEDULER_FIELDS):
            raise ValueError('不是有效的定时任务配置版本')
        if item.get('workspace_key') != self.digest(os.path.normcase(str(Path(workspace).resolve()))):
            raise ValueError('该版本属于其他工作空间，不能在当前空间恢复')
        store = TaskStore(str(Path(workspace) / 'scheduler' / 'tasks.json'))
        task = store.get_task(item['task_id'])
        if task is None:
            raise ValueError('任务已删除，不会自动重新创建任务')
        updates = dict(item['before'])
        restored = dict(task, **updates)
        next_run = SchedulerService(store, lambda t: None)._calculate_next_run(restored, datetime.now())
        if next_run is None:
            raise ValueError('历史执行时间已过期或无效，请在定时任务中编辑新的执行时间')
        updates['next_run_at'] = next_run.isoformat()
        backup = self.record('scheduler', task, restored, '回滚前备份', workspace=workspace)
        store.update_task(item['task_id'], updates)
        return {'message': '定时任务配置已恢复，已重新计算下次执行时间；执行记录保留',
                'backup_id': backup['id'] if backup else None, 'task_id': item['task_id']}

    def read(self, version_id):
        import re
        if not re.fullmatch('[a-f0-9]{32}', str(version_id)):
            raise ValueError('版本编号无效')
        path = self.folder / (version_id + '.json')
        if path.resolve().parent != self.folder.resolve():
            raise ValueError('版本路径无效')
        envelope = json.loads(path.read_text(encoding='utf-8'))
        item = envelope['data']
        if envelope.get('sha256') != self.digest(item):
            raise ValueError('版本完整性校验失败')
        return item

    def list(self, limit=30):
        items = []
        for path in self.folder.glob('*.json'):
            try:
                item = self.read(path.stem)
                items.append({k: v for k, v in item.items() if k not in {'before', 'after'}})
            except (ValueError, KeyError, OSError):
                continue
        return sorted(items, key=lambda x: x['created_at'], reverse=True)[:limit]

    @serialized_task_config
    def rollback(self, version_id):
        from plugins.intelligence_center.task_schema import TASK_FILES, normalize_task, migrate_all
        item = self.read(version_id)
        kind = item['kind']
        if kind not in TASK_FILES or not set(item['before']).issubset(FIELDS):
            raise ValueError('版本类型或字段无效')
        path = self.root / TASK_FILES[kind]
        if path.resolve().parent != self.root:
            raise ValueError('任务文件路径无效')
        data = json.loads(path.read_text(encoding='utf-8'))
        task = next((x for x in data.get('tasks', []) if str(x.get('id')) == item['task_id']), None)
        if task is None:
            raise ValueError('任务已删除，不会自动重新创建或启用任务')
        restored = {k: v for k, v in task.items() if k not in FIELDS}
        restored.update(item['before'])
        restored.update(normalize_task(kind, restored))
        backup = self.record(kind, task, restored, '回滚前备份')
        task.clear()
        task.update(restored)
        self.write(path, data)
        migrate_all(self.root)
        return {'message': '已恢复该任务配置，运行记录和其他任务保持不变',
                'backup_id': backup['id'] if backup else None, 'task_id': item['task_id']}
