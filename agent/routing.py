"""Low-latency model and skill routing for each Agent request.

The router is deliberately deterministic: choosing a model must not require an
extra LLM round-trip. The selected model still receives the full request and
makes the semantic tool/skill decision from the available descriptions.
"""
from __future__ import annotations

import re
import threading
import json
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence


class AgentBindingError(ValueError):
    """Invalid channel-to-Agent binding configuration."""


class AgentUnavailableError(RuntimeError):
    """A binding points at an Agent that is missing or disabled."""


@dataclass(frozen=True)
class _AgentBinding:
    channel_type: str
    agent_id: str
    conversation_id: Optional[str] = None


class AgentRouter:
    """Route inbound channel conversations to isolated Agent workspaces."""

    def __init__(self, registry, bindings: Sequence[_AgentBinding]):
        self.registry = registry
        self.bindings = tuple(bindings)

    @classmethod
    def from_config(cls, settings, registry):
        raw = settings.get("agent_bindings", []) if hasattr(settings, "get") else []
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise AgentBindingError("agent_bindings must be a list")
        bindings = []
        seen = set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise AgentBindingError(f"agent_bindings[{index}] must be an object")
            channel = str(item.get("channel_type") or "").strip()
            agent_id = str(item.get("agent_id") or "").strip()
            conversation = str(item.get("conversation_id") or "").strip() or None
            if not channel or not agent_id:
                raise AgentBindingError(f"agent_bindings[{index}] requires channel_type and agent_id")
            key = (channel, conversation)
            if key in seen:
                raise AgentBindingError(f"duplicate agent binding: {key}")
            seen.add(key)
            bindings.append(_AgentBinding(channel, agent_id, conversation))
        return cls(registry, bindings)

    def _require_available(self, agent_id: str) -> str:
        try:
            return self.registry.get(agent_id, require_enabled=True).id
        except Exception as exc:
            raise AgentUnavailableError(
                f"bound agent '{agent_id}' is missing or disabled"
            ) from exc

    def resolve(self, channel_type: str, conversation_ids=(), explicit_agent_id: str = None) -> str:
        if explicit_agent_id:
            return self._require_available(explicit_agent_id)
        channel = str(channel_type or "").strip()
        ids = {str(value) for value in (conversation_ids or ()) if value is not None}
        # An exact conversation binding always precedes the channel default.
        for binding in self.bindings:
            if binding.channel_type == channel and binding.conversation_id in ids:
                return self._require_available(binding.agent_id)
        for binding in self.bindings:
            if binding.channel_type == channel and binding.conversation_id is None:
                return self._require_available(binding.agent_id)
        return self._require_available(self.registry.default_agent_id)

    def resolve_context(self, context) -> str:
        channel = context.get("channel_type") or context.get("channel") or ""
        candidates = (context.get("session_id"), context.get("receiver"), context.get("conversation_id"))
        agent_id = self.resolve(channel, candidates, context.get("agent_id"))
        context["agent_id"] = agent_id
        return agent_id


_agent_router_lock = threading.Lock()
_agent_router = None
_agent_router_registry = None


def get_agent_router(registry=None):
    """Return the process router, rebuilding when the registry changes."""
    global _agent_router, _agent_router_registry
    if registry is None:
        from agent.registry import get_agent_registry
        registry = get_agent_registry()
    with _agent_router_lock:
        if _agent_router is None or _agent_router_registry is not registry:
            from config import conf
            _agent_router = AgentRouter.from_config(conf(), registry)
            _agent_router_registry = registry
        return _agent_router


@dataclass(frozen=True)
class RouteDecision:
    level: str
    model: Optional[str]
    score: int
    reasons: List[str]
    skill_candidates: List[str]
    agent_candidates: List[str] = None
    intent: str = "general"
    risk: str = "low"
    tool_candidates: List[str] = None
    provider_order: List[str] = None
    fallback_chain: List[str] = None
    memory_hints: List[str] = None


_COMPLEX = (
    "分析", "比较", "对比", "评估", "规划", "方案", "架构", "设计", "根因",
    "排查", "优化", "审计", "安全", "风险", "合同", "法律", "财务", "研究",
    "总结邮件", "整理邮件", "项目", "代码", "开发", "实现", "修复", "测试",
    "analyze", "compare", "evaluate", "plan", "architecture", "design", "debug",
    "security", "audit", "research", "implement", "refactor", "test",
)
_MULTI = ("逐一", "全部", "完整", "综合", "分别", "多个", "先", "然后", "最后", "并且", "以及", "step", "all", "multiple")
_CURRENT_OR_ACTION = ("今天", "最新", "当前", "读取", "搜索", "查找", "发送", "发给", "创建", "修改", "删除", "执行", "打开", "邮件", "文件", "网页")
_FAILURE_SIGNALS = (
    "没反应", "没有反应", "无法", "不能", "失败", "报错", "错误", "异常",
    "不工作", "不可用", "怎么还是", "没办法", "修好", "解决", "安装了",
    "not working", "failed", "error", "broken", "cannot", "can't", "fix",
)
_CONCEPT_ALIASES = {
    "knowledge": ("知识", "知识库", "百科", "索引"),
    "skill": ("技能", "安装skill", "创建skill"),
    "news": ("新闻", "资讯", "简报"),
    "weather": ("天气", "气温", "预报"),
    "ppt": ("ppt", "幻灯片", "演示文稿"),
    "media": ("电影", "音乐", "影视"),
    "hot": ("热搜", "热点", "榜单"),
}

_INTENTS = {
    "weather": ("天气", "气温", "降雨", "weather", "forecast"),
    "email": ("邮件", "邮箱", "收件箱", "email", "mail", "inbox"),
    "file": ("文件", "目录", "本地文档", "查找电脑", "file", "folder"),
    "web": ("搜索", "联网", "网页", "网站", "新闻", "最新", "search", "web", "news"),
    "browser": ("浏览器", "登录", "网页操作", "点击", "表单", "browser", "login"),
    "automation": ("自动化", "定时", "提醒", "监控", "任务", "每天", "每周", "每月", "早上", "晚上", "定期", "schedule", "automation"),
    "code": ("代码", "开发", "实现", "修复", "测试", "部署", "code", "implement", "debug"),
    "memory": ("记忆", "以前", "用户画像", "偏好", "memory", "profile"),
}

_TOOL_HINTS = {
    "weather": ("weather",),
    "email": ("local_email", "send"),
    "file": ("search_files", "read", "ls"),
    "web": ("web_search", "web_fetch", "browser"),
    "browser": ("browser", "web_fetch"),
    "automation": ("scheduler", "send"),
    "code": ("read", "search_files", "edit", "write", "bash"),
    "memory": ("memory_search", "memory_get"),
    "general": (),
}

_AGENT_INTENT_HINTS = {
    "weather": ("网络搜索", "信息获取"),
    "email": ("邮件",),
    "file": ("文件", "本地"),
    "web": ("网络搜索", "信息获取", "财税", "网站更新", "快速学习", "公众号", "网页内容"),
    "browser": ("浏览器", "网页办事"),
    "automation": ("自动化", "网站更新", "外汇", "财税", "网络搜索"),
    "code": ("代码", "开发"),
    "memory": ("记忆", "用户画像"),
    "general": (),
}

_DEFAULT_AGENT_ROSTER = [
    {"id": "network-search", "name": "网络搜索 Agent", "category": "信息获取", "status": "enabled", "entry": "common-function:network-search", "description": "搜索、阅读、过滤、去重并总结网页/RSS/指定站点信息。", "scenarios": ["今日新闻简报", "政策信息搜索", "指定网站核验"], "permissions": ["联网搜索", "读取网页"], "outputs": ["摘要卡片", "简报"]},
    {"id": "quick-learning", "name": "快速学习 Agent", "category": "信息获取", "status": "enabled", "entry": "common-function:quick-learning", "description": "提取公众号文章和网页正文，生成结构化学习笔记并保存产物。", "scenarios": ["公众号文章学习", "网页内容总结", "知识卡片"], "permissions": ["读取网页"], "outputs": ["学习笔记", "知识卡片"]},
    {"id": "email-agent", "name": "邮件处理 Agent", "category": "办公协同", "status": "enabled", "entry": "settings:channels", "description": "读取邮箱，提炼重点、待办和回复草稿。", "scenarios": ["查看今日新邮件", "重要邮件摘要", "邮件回复"], "permissions": ["读取邮箱", "发送邮件需审批"], "outputs": ["邮件摘要", "回复草稿"]},
    {"id": "file-agent", "name": "本地文件 Agent", "category": "本地能力", "status": "enabled", "entry": "common-function:desktop-agent", "description": "查找、读取、整理授权目录中的本地文件。", "scenarios": ["查找桌面文件", "读取文档", "整理资料"], "permissions": ["读取授权目录"], "outputs": ["文件列表", "内容摘要"]},
    {"id": "browser-agent", "name": "浏览器代办 Agent", "category": "网页办事", "status": "enabled", "entry": "common-function:browser-agent", "description": "打开网页、登录、查询后台、填写表单和导出报表。", "scenarios": ["网页登录", "后台查询", "报表导出"], "permissions": ["浏览器操作", "登录需接管"], "outputs": ["查询结果", "操作记录"]},
    {"id": "tax-policy", "name": "财税政策 Agent", "category": "政策情报", "status": "enabled", "entry": "common-preset:tax", "description": "跟踪财税政策并分析影响。", "scenarios": ["最新财税政策", "企业影响分析"], "permissions": ["联网搜索"], "outputs": ["政策摘要", "影响分析"]},
    {"id": "forex-query", "name": "外汇查询 Agent", "category": "金融数据", "status": "enabled", "entry": "common-function:forex-query", "description": "查询外汇牌价并发送通知。", "scenarios": ["美元查询", "汇率提醒"], "permissions": ["读取公开数据源", "发送通知"], "outputs": ["汇率表"]},
    {"id": "research-agent", "name": "个人研究 Agent", "category": "深度研究", "status": "enabled", "entry": "common-function:deep-research", "description": "拆解复杂研究任务并输出决策报告。", "scenarios": ["市场研究", "竞品分析"], "permissions": ["联网搜索", "读取网页"], "outputs": ["研究报告"]},
    {"id": "code-agent", "name": "代码助手 Agent", "category": "开发协同", "status": "enabled", "entry": "common-function:code-agent", "description": "阅读代码、拆解需求、定位错误和测试。", "scenarios": ["修复错误", "新增功能"], "permissions": ["读取项目", "修改代码需授权"], "outputs": ["修复结果"]},
    {"id": "ops-agent", "name": "IT 运维 Agent", "category": "运维告警", "status": "enabled", "entry": "common-function:ops-agent", "description": "检查网站和 API 可用性。", "scenarios": ["健康检查", "异常提醒"], "permissions": ["访问目标地址"], "outputs": ["巡检报告"]},
    {"id": "website-monitor", "name": "网站更新监控 Agent", "category": "变化监控", "status": "enabled", "entry": "common-function:website-monitor", "description": "监控网页变化并通知。", "scenarios": ["Release 监控", "竞品变化"], "permissions": ["读取网页"], "outputs": ["变化摘要"]},
]


def _enabled_agent_candidates(message: str, intent: str, limit: int = 4) -> List[str]:
    """Return enabled Agent-roster hints for this request.

    The roster is user-editable state. Disabled entries are intentionally
    ignored so they are not proactively recommended in model/system prompts.
    """
    try:
        from config import get_data_root
        path = os.path.join(get_data_root(), "intelligence_center", "agent_roster.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            agents = payload.get("agents", []) if isinstance(payload, dict) else []
        else:
            agents = _DEFAULT_AGENT_ROSTER
        if not isinstance(agents, list):
            return []
    except Exception:
        return []
    lowered = (message or "").lower()
    tokens = set(re.findall(r"[a-z0-9_+-]{2,}", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        for width in (2, 3, 4):
            tokens.update(run[i:i + width] for i in range(max(0, len(run) - width + 1)))
    intent_hints = _AGENT_INTENT_HINTS.get(intent, ())
    ranked = []
    for item in agents:
        if not isinstance(item, dict) or item.get("status") != "enabled":
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        haystack = " ".join([
            name,
            str(item.get("category") or ""),
            str(item.get("description") or ""),
            str(item.get("entry") or ""),
            " ".join(str(x) for x in item.get("scenarios") or []),
            " ".join(str(x) for x in item.get("permissions") or []),
            " ".join(str(x) for x in item.get("outputs") or []),
        ]).lower()
        keyword_score = sum(1 for token in tokens if token and token in haystack)
        hint_score = sum(3 for hint in intent_hints if hint.lower() in haystack)
        exact_score = 8 if name.lower() in lowered else 0
        score = keyword_score + hint_score + exact_score
        if score <= 0:
            continue
        # Avoid noisy recommendations caused only by generic words such as
        # “查询/任务/结果”. Intent hints or an explicit name hit can still pass.
        if hint_score <= 0 and exact_score <= 0 and keyword_score < 2:
            continue
        permissions = "、".join(str(x) for x in (item.get("permissions") or [])[:3]) or "按系统权限"
        entry = str(item.get("entry") or "").strip()
        ranked.append((score, f"{name}（{item.get('category') or '未分类'}，入口：{entry or '未配置'}，权限：{permissions}）"))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [text for _, text in ranked[:limit]]


def _configured_connector_candidates(message: str, reliability=None, limit: int = 4) -> List[str]:
    """Return user-configured MCP/CLI connector hints relevant to this request.

    These are not exposed as standalone callable tool names. They are compact
    routing hints so the model can choose an existing execution path such as an
    MCP tool that hot-loaded from mcp.json, or `bash` for a configured CLI.
    """
    try:
        from config import get_data_root
        path = os.path.join(get_data_root(), "integrations", "tool_connectors.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        tools = payload.get("tools", payload) if isinstance(payload, dict) else payload
        if not isinstance(tools, list):
            return []
    except Exception:
        return []
    lowered = (message or "").lower()
    tokens = set(re.findall(r"[a-z0-9_+-]{3,}", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        for width in (2, 3, 4):
            tokens.update(run[i:i + width] for i in range(max(0, len(run) - width + 1)))
    try:
        scores = reliability.scores() if reliability is not None else {}
    except Exception:
        scores = {}
    ranked = []
    for item in tools:
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        name = str(item.get("name") or "").strip()
        typ = "MCP" if item.get("type") == "mcp" else "CLI"
        haystack = f"{name} {item.get('desc','')} {item.get('tags','')} {item.get('command','')}".lower()
        fit = sum(1 for token in tokens if token and token in haystack)
        if not fit and not any(word in lowered for word in ("mcp", "cli", "工具", "github", "agent-reach", "脚本", "命令")):
            continue
        stats = scores.get(f"tool-connector:{item.get('id','')}", {})
        rate = float(stats.get("success_rate", 0.5))
        total = int(stats.get("total", 0))
        recent_failures = int(stats.get("recent_failures", 0))
        usable_bonus = 0.2 if recent_failures == 0 else 0
        score = fit + min(total, 10) * 0.05 + (rate - 0.5) + usable_bonus - recent_failures * 0.45
        usage = "优先寻找已热加载的 MCP 工具" if typ == "MCP" else "可通过 bash 执行 CLI"
        fail_note = f"，最近失败{recent_failures}次" if recent_failures else ""
        ranked.append((score, f"{typ}连接：{name}（{usage}，权限：{'/'.join(item.get('scopes') or ['read'])}，成功率：{round(rate*100)}%{fail_note}）"))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return [text for _, text in ranked[:limit]]


def _compact_hint(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _append_unique(items: List[str], value: str, limit: int = 12) -> None:
    value = _compact_hint(value)
    if value and value not in items and len(items) < limit:
        items.append(value)


def _promote_tool_candidates(candidates: List[str], hints: Sequence[str], intent: str, limit: int = 10) -> List[str]:
    """Move historically successful deterministic tools near the front.

    This does not grant new permissions. It only helps the model prefer paths
    that already worked on this client, such as “先查本地邮件” or “搜索无结果
    后放宽到 7 天”.
    """
    text = " ".join(str(x or "") for x in hints).lower()
    inferred = []
    is_general = intent == "general"
    if intent == "email" or (is_general and any(x in text for x in ("邮件", "邮箱", "thunderbird", "imap", "email", "mail"))):
        inferred.extend(["local_email", "send"])
    if intent == "file" or (is_general and any(x in text for x in ("文件", "索引", "桌面", "授权目录", "file", "index"))):
        inferred.extend(["search_files", "read"])
    if intent == "web" or (intent == "automation" and any(x in text for x in ("搜索", "新闻", "网页", "rss", "来源", "site:", "search"))) or (is_general and any(x in text for x in ("搜索", "新闻", "网页", "rss", "来源", "site:", "search"))):
        inferred.extend(["web_search", "web_fetch"])
    if intent == "browser" or (is_general and any(x in text for x in ("浏览器", "登录", "chrome", "browser", "playwright"))):
        inferred.extend(["browser", "web_fetch"])
    if intent == "automation" or (is_general and any(x in text for x in ("自动化", "定时", "任务", "通知", "scheduler"))):
        inferred.extend(["scheduler", "send"])
    if any(x in text for x in ("微信", "webhook", "邮箱发送", "通知", "补发", "发送")):
        inferred.append("send")
    result = []
    for name in inferred:
        if name and name not in result:
            result.append(name)
    for name in candidates:
        if name and name not in result:
            result.append(name)
    return result[:limit]


def _fact_store_route_hints(message: str, intent: str, limit: int = 6) -> dict:
    """Read compact routing hints from the local fact store."""
    try:
        from config import get_data_root
        from intelligence_center.fact_store import get_fact_store
        store = get_fact_store(os.path.join(get_data_root(), "intelligence_center"))
    except Exception:
        return {}

    query_terms = [message or "", intent or ""]
    type_weights = {
        "success_path": 5,
        "reuse_pattern": 4,
        "failure_lesson": 4,
        "output_preference": 2,
        "source_preference": 2,
        "notification_preference": 2,
        "approval_preference": 2,
        "preference": 1,
    }
    rows = []
    seen = set()
    for query in query_terms:
        try:
            results = store.search(query=query, limit=12) if str(query or "").strip() else []
        except Exception:
            results = []
        for item in results:
            fid = item.get("id") or item.get("fingerprint") or item.get("content")
            if fid in seen:
                continue
            seen.add(fid)
            fact_type = str(item.get("type") or item.get("fact_type") or "")
            if fact_type not in type_weights:
                continue
            score = type_weights.get(fact_type, 0) + int(item.get("seen_count") or 0) * 0.2 + float(item.get("confidence") or 0)
            rows.append((score, item))
    if not rows:
        try:
            for item in store.search(query="", limit=20):
                fact_type = str(item.get("type") or item.get("fact_type") or "")
                if fact_type not in type_weights:
                    continue
                score = type_weights.get(fact_type, 0) + int(item.get("seen_count") or 0) * 0.2 + float(item.get("confidence") or 0)
                rows.append((score, item))
        except Exception:
            pass
    rows.sort(key=lambda x: (x[0], str(x[1].get("updated_at") or "")), reverse=True)

    hints = {"success_paths": [], "failure_lessons": [], "preferences": []}
    for _score, item in rows[: max(limit * 2, 8)]:
        fact_type = str(item.get("type") or item.get("fact_type") or "")
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        hint = _compact_hint(f"{title}：{content}" if title and title not in content else content)
        if not hint:
            continue
        if fact_type in {"success_path", "reuse_pattern"}:
            _append_unique(hints["success_paths"], f"事实库成功经验：{hint}", limit)
        elif fact_type == "failure_lesson":
            _append_unique(hints["failure_lessons"], f"事实库失败经验：{hint}", limit)
        else:
            _append_unique(hints["preferences"], f"事实库偏好：{hint}", limit)
    return hints


def _agent2_route_hints(message: str, intent: str, limit: int = 6) -> dict:
    """Read successful and failed paths learned by Agent 2.0."""
    try:
        from config import conf, get_data_root
        from intelligence_center.agent2_service import Agent2Service
        data_root = get_data_root()
        workspace = conf().get("agent_workspace") or os.path.join(data_root, "cow-workspace")
        service = Agent2Service(os.path.join(data_root, "intelligence_center"), workspace, conf())
    except Exception:
        return {}

    lowered = (message or "").lower()
    wants_weixin = any(x in lowered for x in ("微信", "企业微信", "助理小爱", "手机", "wechat", "weixin"))
    intent_map = {
        "email": ("email", "mail"),
        "file": ("file",),
        "web": ("news", "search", "web"),
        "automation": ("news", "send", "automation"),
        "browser": ("browser",),
    }
    target_types = intent_map.get(intent, ())
    hints = {"success_paths": [], "failure_lessons": []}
    try:
        success_rows = service.weixin_success_learning(limit=20)
    except Exception:
        success_rows = []
    for item in success_rows:
        task_type = str(item.get("task_type") or "")
        if not wants_weixin and target_types and task_type not in target_types:
            continue
        label = item.get("label") or f"{task_type}/{item.get('action') or 'success'}"
        count = int(item.get("count") or 0)
        _append_unique(hints["success_paths"], f"微信成功路径：{label}（成功{count}次）", limit)

    try:
        failure_rows = service.weixin_failure_learning(limit=20)
    except Exception:
        failure_rows = []
    for item in failure_rows:
        category = str(item.get("category") or "")
        lesson = item.get("lesson") or ""
        action = item.get("recommended_action") or ""
        if not wants_weixin and intent == "general":
            continue
        if target_types and not any(t in f"{category} {lesson} {action}".lower() for t in target_types):
            # Keep broadly useful failures such as model/network/browser.
            if category not in {"model_auth", "network", "browser_env", "permission", "empty_result"}:
                continue
        _append_unique(hints["failure_lessons"], f"微信失败经验：{lesson}；建议：{action}", limit)

    try:
        mission = service.mission_command_learning(limit=6)
    except Exception:
        mission = {}
    for item in mission.get("top_commands", []) if isinstance(mission, dict) else []:
        command = item.get("command") or ""
        if command and any(token in command for token in (intent, "检查", "重试", "任务", "微信", "失败")):
            _append_unique(hints["success_paths"], f"总控台常用指令：{command}", limit)
    try:
        self_training = service.self_training_status()
    except Exception:
        self_training = {}
    for item in self_training.get("skill_candidates", []) if isinstance(self_training, dict) else []:
        status = str(item.get("status") or "candidate")
        if status not in {"candidate", "approved"}:
            continue
        text = f"{item.get('title') or ''} {item.get('description') or ''} {item.get('task_type') or ''} {item.get('action') or ''}".lower()
        if target_types and not any(token in text for token in target_types):
            if intent != "general":
                continue
        if lowered and not any(token and token in text for token in re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_+-]{2,}", lowered)):
            if intent != "general":
                continue
        _append_unique(hints["success_paths"], f"自我训练候选 Skill：{item.get('title') or '可复用能力'}（{status}）", limit)
    return hints


def _personal_route_hints(message: str, intent: str, limit: int = 6) -> dict:
    """Read user-authored profile and Memory Tree preferences for routing."""
    try:
        from config import get_data_root
        from intelligence_center.personal_context import personal_route_hints
        return personal_route_hints(os.path.join(get_data_root(), "intelligence_center"), message, intent, limit)
    except Exception:
        return {"preferences": [], "source_domains": [], "topics": []}


def _intent_for(text: str) -> str:
    lowered = text.lower()
    scored = [(sum(1 for word in words if word in lowered), name) for name, words in _INTENTS.items()]
    score, name = max(scored, default=(0, "general"))
    return name if score else "general"


def _tool_candidates(message: str, tools: Optional[Sequence[Any]], intent: str, limit: int = 8, reliability_scores=None) -> List[str]:
    if not tools:
        return list(_TOOL_HINTS.get(intent, ()))[:limit]
    lowered = message.lower()
    tokens = set(re.findall(r"[a-z0-9_+-]{3,}", lowered))
    for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        for width in (2, 3, 4):
            tokens.update(run[i:i + width] for i in range(max(0, len(run) - width + 1)))
    preferred = _TOOL_HINTS.get(intent, ())
    preferred_rank = {name.lower(): idx for idx, name in enumerate(preferred)}
    scored = []
    for tool in tools:
        name = str(getattr(tool, "name", "") or "")
        if not name:
            continue
        name_lower = name.lower()
        haystack = f"{name} {getattr(tool, 'description', '')}".lower()
        score = 12 - preferred_rank[name_lower] if name_lower in preferred_rank else 0
        score += sum(1 for token in tokens if token in haystack)
        if score:
            provider = "mcp" if getattr(tool, "server_name", None) else ("cli" if name == "bash" else "builtin")
            reliability = (reliability_scores or {}).get(name, {})
            quality = float(reliability.get("score", 0.5))
            evidence = min(int(reliability.get("total", 0)), 20)
            recent_failures = int(reliability.get("recent_failures", 0))
            # Semantic fit remains dominant; history only resolves close
            # choices, with influence increasing as evidence accumulates.
            history_bonus = (quality - 0.5) * min(4.0, evidence / 5.0) - recent_failures * 0.6
            scored.append((score + history_bonus, provider != "builtin", name))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    result = [name for _, _, name in scored[:limit]]
    for name in preferred:
        matched = next((getattr(t, "name", "") for t in tools if str(getattr(t, "name", "")).lower() == name.lower()), "")
        if matched and matched not in result:
            result.append(matched)
    return result[:limit]


def _strong_default(global_model: str) -> str:
    lowered = (global_model or "").lower()
    if "gpt-5.6-luna" in lowered or "gpt-5.6-terra" in lowered:
        return "gpt-5.6-sol"
    if lowered.startswith("gpt-5"):
        return "gpt-5.6-sol"
    return global_model


def _skill_candidates(message: str, skill_manager, limit: int = 5) -> List[str]:
    if not skill_manager:
        return []
    query = message.lower()
    # ASCII words plus useful CJK chunks. Exact name hits get a large bonus;
    # descriptions remain the model's authoritative semantic signal.
    tokens = set(re.findall(r"[a-z0-9_+-]{2,}", query))
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        for width in (2, 3, 4):
            tokens.update(run[i:i + width] for i in range(max(0, len(run) - width + 1)))
    scored = []
    try:
        entries = skill_manager.filter_skills(include_disabled=False)
    except Exception:
        entries = []
    for entry in entries:
        skill = entry.skill
        name = str(skill.name or "")
        haystack = f"{name} {skill.description or ''}".lower()
        score = 8 if name.lower() in query else 0
        score += sum(1 for token in tokens if token in haystack)
        for canonical, aliases in _CONCEPT_ALIASES.items():
            if canonical in haystack and any(alias in query for alias in aliases):
                score += 5
        if score:
            scored.append((score, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _, name in scored[:limit]]


def _skill_rollback_penalties() -> dict:
    """Return conservative penalties for self-training Skills the user rolled back."""
    try:
        from config import conf, get_data_root
        from intelligence_center.agent2_service import Agent2Service
        data_root = get_data_root()
        workspace = conf().get("agent_workspace") or os.path.join(data_root, "cow-workspace")
        service = Agent2Service(os.path.join(data_root, "intelligence_center"), workspace, conf())
        rows = service.skill_rollback_summary()
    except Exception:
        return {}
    result = {}
    for item in rows:
        name = str(item.get("skill_name") or "").strip()
        if not name:
            continue
        count = max(1, int(item.get("rollback_count") or 1))
        result[name] = {
            "penalty": min(6.0, 2.5 * count),
            "count": count,
            "last_rollback_at": item.get("last_rollback_at") or "",
        }
    return result


def _self_training_skill_route_hints(message: str, skill_manager, intent: str, reliability_scores=None, limit: int = 4) -> dict:
    """Prefer installed Skills that came from Agent 2.0 self-training.

    A generated Skill is still only a normal project-local Skill. This helper
    does not execute anything or grant permissions; it just lifts matching
    self-training Skills higher in the route so the model is more likely to
    read the right SKILL.md first.
    """
    if not skill_manager:
        return {"names": [], "hints": []}
    try:
        entries = skill_manager.filter_skills(include_disabled=False)
    except Exception:
        entries = []
    if not entries:
        return {"names": [], "hints": []}
    query = (message or "").lower()
    tokens = set(re.findall(r"[a-z0-9_+-]{2,}", query))
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        for width in (2, 3, 4):
            tokens.update(run[i:i + width] for i in range(max(0, len(run) - width + 1)))
    intent_aliases = {
        "email": ("邮件", "邮箱", "email", "mail"),
        "file": ("文件", "目录", "索引", "file"),
        "web": ("搜索", "新闻", "网页", "资讯", "rss", "web"),
        "automation": ("自动化", "定时", "任务", "通知", "automation"),
        "browser": ("浏览器", "登录", "网页操作", "browser"),
        "code": ("代码", "开发", "修复", "code"),
        "memory": ("记忆", "用户画像", "memory"),
    }.get(intent, ())
    rollback_penalties = _skill_rollback_penalties()
    scored = []
    for entry in entries:
        skill = entry.skill
        name = str(skill.name or "")
        content = str(skill.content or "")
        frontmatter = skill.frontmatter if isinstance(skill.frontmatter, dict) else {}
        haystack = f"{name} {skill.description or ''} {content[:3000]}".lower()
        is_self_training = (
            str(frontmatter.get("origin") or "").lower() == "self_training"
            or "self-training" in name.lower()
            or "agent 2.0 自我训练" in content
            or "自我训练沉淀" in content
        )
        if not is_self_training:
            continue
        score = 4
        if name.lower() in query:
            score += 8
        score += sum(1 for token in tokens if token and token in haystack)
        score += sum(4 for alias in intent_aliases if alias.lower() in haystack)
        history = (reliability_scores or {}).get(f"skill:{name}", {})
        if history:
            quality = float(history.get("score", 0.5))
            total = int(history.get("total", 0))
            recent_failures = int(history.get("recent_failures", 0))
            score += (quality - 0.5) * min(8.0, max(1, total) / 2.0)
            score -= recent_failures * 1.2
        rollback_info = rollback_penalties.get(name) or {}
        if rollback_info:
            score -= float(rollback_info.get("penalty") or 0)
        if score <= 4 and intent != "general":
            continue
        scored.append((score, name, _compact_hint(skill.description or content.replace("\n", " "), 90), history, rollback_info))
    scored.sort(key=lambda item: (-item[0], item[1]))
    names = []
    hints = []
    for score, name, description, history, rollback_info in scored[:limit]:
        if name not in names:
            names.append(name)
            history_text = ""
            if history:
                history_text = f"，历史成功率{round(float(history.get('success_rate', 0.5))*100)}%，最近失败{int(history.get('recent_failures') or 0)}次"
            rollback_text = ""
            if rollback_info:
                rollback_text = f"，曾回滚{int(rollback_info.get('count') or 1)}次，已降权"
            hints.append(f"自我训练正式 Skill：{name}（匹配度{round(score, 2)}{history_text}{rollback_text}，{description or '来自本机经验沉淀'}）")
    return {"names": names, "hints": hints}


def route_request(message: str, global_model: str, skill_manager=None, fast_model: str = "", strong_model: str = "", tools=None, reliability=None) -> RouteDecision:
    text = (message or "").strip()
    lowered = text.lower()
    score = 0
    reasons: List[str] = []
    complex_hits = sum(1 for word in _COMPLEX if word in lowered)
    if complex_hits:
        score += min(4, complex_hits * 2); reasons.append("深度分析或专业任务")
    if len(text) >= 240:
        score += 2; reasons.append("上下文较长")
    elif len(text) >= 100:
        score += 1
    if sum(1 for word in _MULTI if word in lowered) >= 2:
        score += 2; reasons.append("包含多个步骤或交付要求")
    if any(word in lowered for word in _CURRENT_OR_ACTION):
        score += 1; reasons.append("需要检索或执行操作")
    if any(word in lowered for word in _FAILURE_SIGNALS):
        score += 3; reasons.append("需要诊断并直接恢复")
    if "[图片:" in text or "[文件:" in text or "attachment" in lowered:
        score += 2; reasons.append("包含附件或图片")

    intent = _intent_for(text)
    risky = any(word in lowered for word in ("发送", "发给", "删除", "写入", "修改", "执行", "安装", "登录", "购买", "发布", "实现", "修复", "部署", "send", "delete", "write", "execute", "install", "login", "implement", "fix", "deploy"))
    risk = "high" if any(word in lowered for word in ("删除", "购买", "发布", "付款", "delete", "purchase", "publish")) else ("medium" if risky else "low")
    if intent in {"browser", "file", "email", "automation"} and risky:
        score += 2
        reasons.append("需要工具闭环执行")
    if any(word in lowered for word in ("登录", "扫码", "验证码", "二次验证", "login", "captcha", "mfa")):
        score += 2
        reasons.append("涉及登录或人工接管点")
    level = "strong" if score >= 3 else "fast"
    chosen = (strong_model or _strong_default(global_model)) if level == "strong" else (fast_model or global_model)
    reliability_scores = reliability.scores(intent) if reliability is not None else {}
    candidates = _tool_candidates(text, tools, intent, reliability_scores=reliability_scores)
    for connector in _configured_connector_candidates(text, reliability=reliability):
        if connector not in candidates:
            candidates.append(connector)
    candidates = candidates[:10]
    provider_order = ["builtin", "mcp", "cli"] if intent not in {"code", "browser", "file"} else ["builtin", "cli", "mcp"]
    learned_paths = reliability.successful_paths(intent, 2) if reliability is not None else []
    fallback = [" → ".join(path) for path in learned_paths]
    fact_hints = _fact_store_route_hints(text, intent)
    agent2_hints = _agent2_route_hints(text, intent)
    personal_hints = _personal_route_hints(text, intent)
    memory_hints = []
    learned_hint_texts = []
    for group in (fact_hints.get("success_paths", []), agent2_hints.get("success_paths", []),
                  fact_hints.get("failure_lessons", []), agent2_hints.get("failure_lessons", []),
                  fact_hints.get("preferences", []), personal_hints.get("preferences", [])):
        for item in group:
            _append_unique(learned_hint_texts, item, 10)
            _append_unique(fallback, item, 10)
            if str(item or "").startswith("用户画像"):
                _append_unique(memory_hints, item, 8)
    self_training_skill_hints = _self_training_skill_route_hints(text, skill_manager, intent, reliability_scores=reliability_scores)
    for item in self_training_skill_hints.get("hints", []):
        _append_unique(learned_hint_texts, item, 10)
        _append_unique(fallback, item, 10)
    if learned_hint_texts:
        candidates = _promote_tool_candidates(candidates, learned_hint_texts, intent)
        reasons.append("已复用本机成功/失败经验")
    if personal_hints.get("preferences"):
        reasons.append("已应用用户画像/记忆树偏好")
        if intent in {"web", "automation"} and "web_search" not in candidates:
            candidates.insert(0, "web_search")
        if intent == "email" and "local_email" not in candidates:
            candidates.insert(0, "local_email")
        candidates = candidates[:10]
    if not fallback:
        fallback = ["首选候选", "同类可用工具", "通用模型直接回答"]
    agent_candidates = _enabled_agent_candidates(text, intent)
    if agent_candidates:
        reasons.append("已匹配启用的 Agent 名册")
    skill_candidates = []
    for name in self_training_skill_hints.get("names", []):
        if name not in skill_candidates:
            skill_candidates.append(name)
    for name in _skill_candidates(text, skill_manager):
        if name not in skill_candidates and len(skill_candidates) < 5:
            skill_candidates.append(name)
    if self_training_skill_hints.get("names"):
        reasons.append("已优先匹配自我训练正式 Skill")
    return RouteDecision(level, chosen, score, reasons or ["直接问答"], skill_candidates, agent_candidates, intent, risk, candidates, provider_order, fallback, memory_hints)
