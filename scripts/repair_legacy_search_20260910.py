"""Explicit four-task repair through the running backend; no task execution/sends.

Default is preview. --apply uses the existing versioned task update endpoint.
Run from the project root. Never prints notification credentials.
"""
import argparse
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


TOPICS = {
    'f84aac62811c': ['国内热点'],
    '59337ae0fad2': ['AI', 'IT'],
    'a82fad7a18f3': ['美元指数', '人民币兑美元汇率', '美联储动态'],
    '974ed1ff4803': ['最新财税政策', '税务', '财政'],
}


def repair_patch(task):
    topics = TOPICS[task['id']]
    patch = {'keywords': topics}
    if task['id'] != 'f84aac62811c':
        sources = task.get('sources') or []
        if len(sources) != 1:
            raise ValueError('来源数量已变化，停止自动修改')
        url = urlsplit(sources[0])
        pairs = parse_qsl(url.query, keep_blank_values=True)
        queries = [v for k, v in pairs if k == 'q']
        if url.hostname != 'news.google.com' or len(queries) != 1:
            raise ValueError('不是预期的单一 Google RSS 查询')
        # Keep explicit site/date operators, not instruction fragments or stray domains.
        operators = [v for v in queries[0].split() if v.startswith(('site:', 'when:', 'after:', 'before:'))]
        query = ' '.join(topics + operators)
        pairs = [(k, query if k == 'q' else v) for k, v in pairs]
        patch['sources'] = [urlunsplit(url._replace(query=urlencode(pairs)))]
    rules = deepcopy(task.get('rules') or {})
    for key in ('include', 'exclude'):
        rules[key] = [v for v in rules.get(key, []) if v not in ('无', '无强制词')]
    if rules != task.get('rules'):
        patch['rules'] = rules
    return {k: v for k, v in patch.items() if task.get(k) != v}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    path = Path('intelligence_center/news_tasks.json')
    before = json.loads(path.read_text(encoding='utf-8'))
    tasks = {t['id']: t for t in before['tasks']}
    plans = {key: repair_patch(tasks[key]) for key in TOPICS}
    for key, patch in plans.items():
        print(json.dumps({'task': tasks[key]['name'], 'keywords': TOPICS[key], 'fields': list(patch)}, ensure_ascii=False))
    if not args.apply:
        return
    for key, patch in plans.items():
        if not patch:
            continue
        latest = {t['id']: t for t in json.loads(path.read_text(encoding='utf-8'))['tasks']}[key]
        if any(latest.get(k) != tasks[key].get(k) for k in ('keywords', 'sources', 'rules')):
            raise RuntimeError('任务被其他操作修改，停止修复')
        payload = json.dumps({'kind': 'news', 'task_id': key, 'patch': patch}).encode()
        request = Request('http://127.0.0.1:9899/api/intelligence/hub/task', data=payload,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get('status') != 'success':
            raise RuntimeError('任务更新失败，请查看后端日志')
    after = {t['id']: t for t in json.loads(path.read_text(encoding='utf-8'))['tasks']}
    for key, old in tasks.items():
        new = after[key]
        allowed = {'keywords', 'sources', 'rules', 'definition', 'updated_at'} if key in TOPICS else set()
        assert all(old.get(k) == new.get(k) for k in old.keys() | new.keys() if k not in allowed), key
        if key in TOPICS:
            assert new['keywords'] == TOPICS[key]
            assert new['definition']['conditions']['keywords'] == TOPICS[key]
            for k in ('include', 'exclude'):
                expected = [v for v in (old.get('rules') or {}).get(k, []) if v not in ('无', '无强制词')]
                assert new['rules'][k] == expected
            assert not repair_patch(new), '修复必须可重复执行且不产生新改动'
    print('PASS: 4 tasks verified; schedules, enabled states, notifications and other tasks unchanged.')


if __name__ == '__main__':
    main()
