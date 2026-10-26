"""Public execution facts only; never persist prompts, replies or credentials."""
import json
import threading
import uuid
from collections import deque
from datetime import datetime
from functools import wraps
from pathlib import Path

from common.runtime_identity import current_identity, identity_scope

_lock = threading.RLock()


def emit(context, stage, status, **facts):
    try:
        from config import get_data_root
        context = context if context is not None else {}
        run_id = context.get('trace_id') or current_identity().run_id
        if not run_id:
            return
        event = {'id': run_id, 'at': datetime.now().astimezone().isoformat(),
                 'source': context.get('channel_type') or context.get('channel') or '',
                 'session_id': context.get('session_id') or current_identity().session_id or '',
                 'source_id': context.get('remote_bridge_command_id') or context.get('request_id') or '',
                 'stage': stage, 'status': status}
        # Deliberately no free-text error, user message, cookies or destination URLs.
        event.update({k: v for k, v in facts.items() if k in {'artifact_id', 'notification_written', 'device_received'}})
        path = Path(get_data_root()) / 'intelligence_center' / 'execution_trace.jsonl'
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a', encoding='utf-8') as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + '\n')
    except Exception:
        pass  # Observability cannot interrupt the actual task.


def traced(stage, context_position):
    def decorate(fn):
        @wraps(fn)
        def call(*args, **kwargs):
            context = kwargs.get('context')
            if context is None and len(args) > context_position:
                context = args[context_position]
            if context is None:
                return fn(*args, **kwargs)
            if not context.get('trace_id'):
                context['trace_id'] = current_identity().run_id or uuid.uuid4().hex
            with identity_scope(run_id=context.get('trace_id'), session_id=context.get('session_id')):
                emit(context, stage, 'running')
                try:
                    result = fn(*args, **kwargs)
                    status = 'completed'
                    if stage == '生成回复':
                        status = 'generated' if getattr(result, 'content', None) else 'empty'
                        from bridge.reply import ReplyType
                        if getattr(result, 'type', None) == ReplyType.ERROR:
                            status = 'failed'
                    emit(context, stage, status)
                    return result
                except Exception:
                    emit(context, stage, 'failed')
                    raise
        return call
    return decorate


def read_events(root):
    events = []
    for name in ('execution_trace.previous.jsonl', 'execution_trace.jsonl'):
        try:
            with (Path(root) / name).open(encoding='utf-8') as handle:
                lines = deque(handle, maxlen=3000)
            for line in lines:
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get('id'):
                        events.append(event)
                except ValueError:
                    continue
        except OSError:
            continue
    return events


def confirm_remote_result(item, device_id, status='received'):
    """Validate a receipt against a completed, device-owned result."""
    if not item.get('source_device_id') or str(item['source_device_id']) != str(device_id):
        raise ValueError('该结果不属于当前远程设备，不能确认回执')
    if item.get('status') != 'done':
        raise ValueError('任务尚未完成，不能确认结果已收到')
    if status != 'received':
        raise ValueError('仅支持 received 回执；执行成功与设备收到结果是不同状态')


def emit_remote_receipt(item):
    emit({'trace_id': item.get('trace_id'), 'channel': 'remote_bridge',
          'session_id': 'remote_bridge:' + str(item.get('id')),
          'remote_bridge_command_id': item.get('id')},
         '设备确认收到', 'received', device_received=True)


def weixin_entry(fn):
    @wraps(fn)
    def call(self, raw_msg, from_user, context_token):
        context = {'trace_id': uuid.uuid4().hex, 'channel_type': 'weixin',
                   'session_id': self._conversation_session_id(from_user),
                   'request_id': str(raw_msg.get('message_id') or raw_msg.get('seq') or '')}
        with identity_scope(run_id=context['trace_id'], session_id=context['session_id']):
            emit(context, '微信授权消息接入', 'running')
            try:
                return fn(self, raw_msg, from_user, context_token)
            except Exception:
                emit(context, '微信消息执行', 'failed')
                raise
    return call


def send_observer(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            emit(None, '微信接口发送', 'accepted' if result is True else 'failed' if result is False else 'unconfirmed',
                 notification_written=result if isinstance(result, bool) else None)
            return result
        except Exception:
            emit(None, '微信接口发送', 'failed', notification_written=False)
            raise
    return call
