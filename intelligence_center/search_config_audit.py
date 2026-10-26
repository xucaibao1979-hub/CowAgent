"""Read-only legacy search audit; never silently broaden sources or dates."""
from urllib.parse import urlparse, parse_qs


INSTRUCTION_WORDS = ('通过可用搜索引擎', '时间范围最近', '必须包含', '最终严格',
                     '执行结果发送', '禁止输出', '输出格式', '自动执行网络搜索')


def inspect_search_task(task):
    issues = []
    definition = task.get('definition') or {}
    keywords = task.get('keywords', (definition.get('conditions') or {}).get('keywords', []))
    fields = [('keywords', ' '.join(str(x) for x in keywords))]
    for index, source in enumerate(task.get('sources', definition.get('sources', [])) or []):
        if isinstance(source, dict):
            source = source.get('value')
        if not isinstance(source, str):
            continue
        query = parse_qs(urlparse(source).query).get('q', [])
        if query:
            fields.append((f'sources[{index}].q', ' '.join(query)))
    for field, value in fields:
        hits = [word for word in INSTRUCTION_WORDS if word in value]
        if hits:
            issues.append({'field': field, 'reason': '搜索词混入执行指令', 'markers': hits,
                           'suggestion': '仅保留主题；来源、时间、排除词和输出要求仍保留在各自配置中'})
    return issues
