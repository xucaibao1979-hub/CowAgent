"""Lightweight structured decision engine for CowAgent automation.

This is a local System-One-style layer inspired by Jev: it does not generate
prose.  It turns fuzzy runtime state into small typed decisions that normal
code can execute quickly and consistently.  The functions are deterministic
and cheap, so they are safe to call in high-frequency scheduler, notification
and formatting paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List


GENERIC_SUMMARY_PATTERNS = (
    "出现新的进展",
    "可继续跟踪",
    "相关企业反应和行业影响",
    "适合跟踪能力变化",
    "近期相关领域的新变化",
    "有新的公开信息",
    "建议重点看核心事实",
)


DOMESTIC_HINTS = (
    "中国", "国内", "北京", "上海", "深圳", "广州", "杭州", "南京", "成都",
    "华为", "小米", "腾讯", "阿里", "字节", "百度", "京东", "美团", "蚂蚁",
    "工信部", "网信办", "发改委", "财政部", "税务总局", "证监会", "国家",
)

INTERNATIONAL_HINTS = (
    "美国", "欧洲", "欧盟", "英国", "日本", "韩国", "印度", "海外", "国际",
    "OpenAI", "Anthropic", "Google", "Microsoft", "NVIDIA", "Apple", "Meta",
    "Amazon", "Reuters", "TechCrunch", "The Verge", "FCC", "SEC", "White House",
)

TOPIC_KEYWORDS = {
    "ai": ("AI", "人工智能", "大模型", "模型", "智能体", "Agent", "OpenAI", "Anthropic"),
    "chip": ("芯片", "半导体", "GPU", "NPU", "先进制程", "台积电", "英伟达", "NVIDIA"),
    "cloud": ("数据中心", "云计算", "算力", "服务器", "AIDC", "基础设施"),
    "security": ("网络安全", "数据安全", "漏洞", "攻击", "泄露", "勒索", "合规"),
    "policy": ("监管", "政策", "法案", "出口管制", "制裁", "许可", "规则"),
    "company": ("发布", "推出", "升级", "财报", "融资", "收购", "合作"),
}


@dataclass
class ContentQualityDecision:
    title_body_duplicate: bool
    summary_too_generic: bool
    region: str
    has_real_fact: bool
    action: str
    confidence: float
    reason: str


@dataclass
class NotificationDecision:
    primary_channel: str
    fallback_channel: str
    should_send_primary: bool
    should_enqueue_retry: bool
    should_alert_user: bool
    severity: str
    confidence: float
    reason: str
    actions: List[str]


@dataclass
class TaskPreflightDecision:
    can_save: bool
    can_run_next_time: bool
    notification_risk: str
    missing_items: List[str]
    recommended_fallback: str
    confidence: float
    reason: str
    actions: List[str]


@dataclass
class ArchiveDecision:
    category: str
    importance: float
    should_keep_daily: bool
    should_promote_to_weekly: bool
    reason: str


@dataclass
class FailureDecision:
    failure_type: str
    root_cause: str
    auto_fixable: bool
    severity: str
    confidence: float
    suggested_action: str
    actions: List[str]


def _clean(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text


def _key(value: Any) -> str:
    return re.sub(r"[\W_]+", "", _clean(value).lower())


def _similarity(a: Any, b: Any) -> float:
    ak, bk = _key(a), _key(b)
    if not ak or not bk:
        return 0.0
    if ak in bk or bk in ak:
        return 0.92
    return SequenceMatcher(None, ak, bk).ratio()


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(str(item).lower() in lowered for item in needles if str(item or "").strip())


class DecisionEngine:
    """Deterministic typed decisions for Agent workflow control."""

    @staticmethod
    def content_quality(title: str, summary: str, url: str = "", context: str = "") -> Dict[str, Any]:
        material = " ".join([_clean(title), _clean(summary), _clean(context), _clean(url)])
        duplicate = _similarity(title, summary) >= 0.82
        generic = _contains_any(summary, GENERIC_SUMMARY_PATTERNS) or len(_clean(summary)) < 18
        has_fact = bool(
            re.search(r"\d", material)
            or re.search(r"[A-Za-z]{2,}", material)
            or re.search(r"(发布|推出|宣布|监管|投资|融资|收购|漏洞|攻击|制裁|批准|升级|开源|合作)", material)
        )
        region = DecisionEngine.classify_region(material)
        if duplicate or generic or not has_fact:
            action = "regenerate"
        else:
            action = "accept"
        confidence = 0.92 if action == "accept" else 0.86
        reasons = []
        if duplicate:
            reasons.append("标题和正文重复")
        if generic:
            reasons.append("正文描述过泛")
        if not has_fact:
            reasons.append("缺少具体事实")
        return asdict(ContentQualityDecision(
            title_body_duplicate=duplicate,
            summary_too_generic=generic,
            region=region,
            has_real_fact=has_fact,
            action=action,
            confidence=confidence,
            reason="；".join(reasons) or "内容质量通过",
        ))

    @staticmethod
    def classify_region(text: str) -> str:
        material = _clean(text)
        domestic = sum(1 for item in DOMESTIC_HINTS if item.lower() in material.lower())
        international = sum(1 for item in INTERNATIONAL_HINTS if item.lower() in material.lower())
        if domestic > international:
            return "domestic"
        if international > domestic:
            return "international"
        return "unknown"

    @staticmethod
    def classify_news_item(item: Dict[str, Any], seen_keys: Iterable[str] = ()) -> Dict[str, Any]:
        title = _clean(item.get("title"))
        summary = _clean(item.get("summary") or item.get("description"))
        link = _clean(item.get("link") or item.get("url"))
        material = " ".join([title, summary, link])
        topics = [name for name, terms in TOPIC_KEYWORDS.items() if _contains_any(material, terms)]
        if not topics:
            topics = ["general"]
        quality = DecisionEngine.content_quality(title, summary, link)
        dedupe_key = _key(title)[:80] or _key(link)[:80]
        duplicate = bool(dedupe_key and dedupe_key in set(seen_keys or []))
        score = 0.35
        score += 0.2 if quality.get("has_real_fact") else 0
        score += 0.15 if link else 0
        score += 0.15 if topics != ["general"] else 0
        score -= 0.25 if duplicate else 0
        score -= 0.2 if quality.get("summary_too_generic") else 0
        return {
            "region": quality.get("region") or "unknown",
            "topics": topics,
            "is_duplicate": duplicate,
            "dedupe_key": dedupe_key,
            "importance": round(max(0.0, min(1.0, score)), 3),
            "worth_attention": score >= 0.55 and not duplicate,
            "quality": quality,
        }

    @staticmethod
    def notification_route(target: Dict[str, Any], health: Dict[str, Any], pending_count: int = 0,
                           task: Dict[str, Any] | None = None) -> Dict[str, Any]:
        channel = _clean((target or {}).get("channel") or (target or {}).get("type"))
        if channel == "channel":
            channel = _clean((target or {}).get("name"))
        ok = bool((health or {}).get("ok"))
        configured = bool((health or {}).get("configured") or (health or {}).get("active"))
        token_ready = (health or {}).get("token_ready")
        message = _clean((health or {}).get("message"))
        fallback = "web"
        if channel in {"web", ""}:
            return asdict(NotificationDecision(channel or "web", fallback, True, False, False, "info", 0.98, "Web 兜底通道可用", ["persist_web"]))
        should_enqueue = channel in {"weixin", "wecom_bot"} and (not ok or token_ready is False)
        severity = "info"
        should_alert = False
        if not configured:
            severity = "error"
            should_alert = True
        elif not ok:
            severity = "warning"
            should_alert = pending_count >= 3
        if token_ready is False:
            severity = "warning"
        actions: List[str] = []
        if ok:
            actions.append("send_primary")
            if pending_count:
                actions.append("retry_pending")
        else:
            if channel in {"weixin", "wecom_bot"}:
                actions.append("try_reconnect")
            if should_enqueue:
                actions.append("queue_retry")
            actions.append("web_fallback")
        if should_alert:
            actions.append("alert_user")
        return asdict(NotificationDecision(
            primary_channel=channel,
            fallback_channel=fallback,
            should_send_primary=ok,
            should_enqueue_retry=should_enqueue,
            should_alert_user=should_alert,
            severity=severity,
            confidence=0.9 if configured else 0.82,
            reason=message or ("通道可发送" if ok else "通道不可用，使用兜底策略"),
            actions=list(dict.fromkeys(actions)),
        ))

    @staticmethod
    def task_preflight(payload: Dict[str, Any], checks: List[Dict[str, Any]], schedule_preview: Dict[str, Any]) -> Dict[str, Any]:
        missing: List[str] = []
        risk = "low"
        can_run = bool((schedule_preview or {}).get("next_run"))
        for check in checks or []:
            status = _clean(check.get("status"))
            channel = _clean(check.get("channel"))
            if status == "error":
                risk = "high"
                missing.append(channel or _clean(check.get("name")) or "notification_channel")
            elif status == "warning" and risk != "high":
                risk = "medium"
            if channel == "weixin" and not check.get("can_send"):
                missing.append("weixin_session_refresh")
        can_save = risk != "high" or bool(payload.get("allow_save_with_warnings"))
        actions: List[str] = []
        if risk != "low":
            actions.extend(["keep_web_fallback", "use_recommended_fallback"])
        if "weixin_session_refresh" in missing:
            actions.append("refresh_weixin_session")
        if risk == "high":
            actions.append("block_or_confirm")
        return asdict(TaskPreflightDecision(
            can_save=can_save,
            can_run_next_time=can_run,
            notification_risk=risk,
            missing_items=list(dict.fromkeys(x for x in missing if x)),
            recommended_fallback="wecom_bot" if risk != "low" else "web",
            confidence=0.88,
            reason="预检通过" if risk == "low" else "存在通道或调度风险，建议保存前处理或启用兜底",
            actions=list(dict.fromkeys(actions or ["allow_save"])),
        ))

    @staticmethod
    def archive_item(item: Dict[str, Any], source: str = "") -> Dict[str, Any]:
        text = " ".join([_clean(item.get("title")), _clean(item.get("summary")), _clean(item.get("status")), _clean(source)])
        if re.search(r"失败|错误|异常|报错|不可用|断开|过期|重复|No module|failed|error|blocked", text, re.I):
            category = "问题类"
            importance = 0.86
        elif re.search(r"读书|学习|笔记|方法|总结|课程|书籍|quick", text, re.I):
            category = "学习类"
            importance = 0.72
        elif re.search(r"优化|升级|进化|训练|评测|Harness|自检|修复|决策", text, re.I):
            category = "进化类"
            importance = 0.78
        elif re.search(r"简报|日报|自动化|任务|产物|交付|报告", text, re.I):
            category = "任务产物类"
            importance = 0.66
        else:
            category = "知识类"
            importance = 0.58
        return asdict(ArchiveDecision(
            category=category,
            importance=importance,
            should_keep_daily=True,
            should_promote_to_weekly=importance >= 0.72,
            reason=f"基于标题、摘要和来源 {source or 'unknown'} 分类",
        ))

    @staticmethod
    def failure_root_cause(message: str, context: Dict[str, Any] | None = None) -> Dict[str, Any]:
        text = " ".join([_clean(message), _clean((context or {}).get("channel")), _clean((context or {}).get("task_name"))])
        rules = [
            ("module_missing", "packaged_backend_missing_module", True, "error", "重新打包后端并重启应用", r"No module named|ModuleNotFoundError|import"),
            ("scheduler_lock", "stale_running_lock", True, "warning", "清理过期运行锁并允许重试", r"运行锁|旧锁|running|未完成的执行记录|stale"),
            ("config_mismatch", "runtime_config_missing", True, "error", "迁移配置到当前用户数据目录并重启", r"配置|未启用|未配置|missing|不存在|凭证"),
            ("channel_expired", "weixin_session_expired", True, "warning", "刷新微信会话或重新扫码", r"微信|weixin|扫码|发送令牌|context_token|会话.*过期|waiting_scan"),
            ("channel_disconnected", "wecom_long_link_disconnected", True, "warning", "自动重连企业微信长连接，失败则切 Web 兜底", r"企微|企业微信|wecom|长连接|WebSocket|takeover|接管|未连接"),
            ("webhook_mismatch", "webhook_request_not_matched", True, "warning", "检查 Webhook URL、方法和签名配置", r"Webhook|请求方法不匹配|HTTP 4|HTTP 5|签名"),
        ]
        for failure_type, root_cause, auto_fixable, severity, action, pattern in rules:
            if re.search(pattern, text, re.I):
                actions = {
                    "module_missing": ["rebuild_backend", "restart_backend"],
                    "scheduler_lock": ["cleanup_stale_locks", "retry_task"],
                    "config_mismatch": ["sync_config", "restart_backend"],
                    "channel_expired": ["refresh_weixin_session", "queue_retry", "web_fallback"],
                    "channel_disconnected": ["try_reconnect", "queue_retry", "web_fallback"],
                    "webhook_mismatch": ["test_webhook", "web_fallback"],
                }.get(failure_type, ["human_review"])
                return asdict(FailureDecision(failure_type, root_cause, auto_fixable, severity, 0.88, action, actions))
        return asdict(FailureDecision("unknown", "needs_human_review", False, "warning", 0.55, "保留运行记录并提示用户查看详情", ["human_review"]))


def decision_engine() -> DecisionEngine:
    return DecisionEngine()
