"""Small, safe helpers that turn the personal profile into usable defaults.

The profile is user-authored memory.  Treat it as preference data only: it can
suggest topics, sources and output style, but it must not grant permissions or
silently perform high-risk actions.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote


AI_SOURCE_DOMAINS = [
    "36kr.com", "qbitai.com", "jiqizhixin.com", "infoq.cn", "sspai.com",
    "openai.com", "anthropic.com", "deepmind.google", "huggingface.co",
    "arxiv.org", "github.com",
]
FISCAL_SOURCES = [
    "https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/",
    "https://fgk.chinatax.gov.cn/",
    "https://www.chinatax.gov.cn/chinatax/index.html",
    "https://www.gov.cn/zhengce/zuixin/",
]
TECH_SOURCE_DOMAINS = [
    "36kr.com", "infoq.cn", "ithome.com", "sspai.com", "ifanr.com",
    "juejin.cn", "oschina.net", "segmentfault.com", "cloud.tencent.com",
    "developer.aliyun.com",
]


def load_personal_context(data_dir) -> dict:
    try:
        from intelligence_center.personal_agent_service import PersonalAgentService
        profile = PersonalAgentService(data_dir).get()
    except Exception:
        profile = {}
    material = " ".join([
        str(profile.get("role") or ""),
        str(profile.get("custom_role") or ""),
        str(profile.get("industry") or ""),
        " ".join(profile.get("goals") or []) if isinstance(profile.get("goals"), list) else str(profile.get("goals") or ""),
        " ".join(profile.get("preferred_keywords") or []) if isinstance(profile.get("preferred_keywords"), list) else str(profile.get("preferred_keywords") or ""),
        " ".join(profile.get("preferred_sources") or []) if isinstance(profile.get("preferred_sources"), list) else str(profile.get("preferred_sources") or ""),
        str(profile.get("custom_needs") or ""),
        str(profile.get("output_preferences") or ""),
    ]).lower()
    topics = []
    if any(token in material for token in ("ai", "agent", "智能体", "人工智能", "大模型")):
        topics.extend(["AI", "AI Agent", "大模型", "自动化"])
    if any(token in material for token in ("创业", "产品", "科技", "互联网")):
        topics.extend(["创业", "科技", "产品"])
    if any(token in material for token in ("财税", "财政", "税务", "发票", "政策")):
        topics.extend(["财税政策", "税务", "财政"])
    if any(token in material for token in ("代码", "开发", "工程", "运维", "mcp", "cli")):
        topics.extend(["开发者工具", "开源项目", "MCP", "CLI"])
    domains = []
    if any(token in topics for token in ("AI", "AI Agent", "大模型", "自动化")):
        domains.extend(AI_SOURCE_DOMAINS)
    if any(token in topics for token in ("创业", "科技", "产品", "开发者工具", "开源项目", "MCP", "CLI")):
        domains.extend(TECH_SOURCE_DOMAINS)
    source_material = " ".join([
        " ".join(profile.get("preferred_sources") or []) if isinstance(profile.get("preferred_sources"), list) else str(profile.get("preferred_sources") or ""),
        str(profile.get("custom_needs") or ""),
        str(profile.get("output_preferences") or ""),
    ])
    explicit_urls = re.findall(r"https?://[^\s，,；;]+", source_material)
    explicit_domains = re.findall(r"(?<!@)\b[a-z0-9.-]+\.(?:com|cn|net|org|io|ai|google)\b", source_material, re.I)
    domains.extend(explicit_domains)
    preferred_keywords = [str(x).strip() for x in (profile.get("preferred_keywords") or []) if str(x).strip()] if isinstance(profile.get("preferred_keywords"), list) else [x.strip() for x in re.split(r"[\n,，;；]+", str(profile.get("preferred_keywords") or "")) if x.strip()]
    if preferred_keywords:
        topics = list(dict.fromkeys(preferred_keywords + topics))
    output_preferences = str(profile.get("output_preferences") or "").strip()
    return {
        "profile": profile,
        "topics": list(dict.fromkeys(topics)),
        "source_domains": list(dict.fromkeys(domain.lower().removeprefix("www.") for domain in domains if domain)),
        "source_urls": list(dict.fromkeys(explicit_urls)),
        "fiscal_sources": FISCAL_SOURCES if any(token in topics for token in ("财税政策", "税务", "财政")) else [],
        "output_preferences": output_preferences,
        "summary": _summary(profile, topics, domains, output_preferences),
    }


def personal_routing_context(data_dir, message: str = "", intent: str = "", max_chars: int = 900) -> str:
    """Return a compact per-request memory hint for the runtime prompt.

    This is intentionally small.  It helps the Agent apply the user's stable
    preferences without pasting the whole Memory Wiki into every request.
    Preferences are not permissions: risky actions still require the existing
    approval flow.
    """
    context = load_personal_context(data_dir)
    profile = context.get("profile") or {}
    lowered = f"{message or ''} {intent or ''}".lower()
    lines = []
    summary = context.get("summary") or ""
    if summary:
        lines.append(f"- 用户画像摘要：{_clip(summary, 180)}")
    output_preferences = str(profile.get("output_preferences") or context.get("output_preferences") or "").strip()
    if output_preferences:
        lines.append(f"- 输出偏好：{_clip(output_preferences, 160)}")
    wants_email = intent == "email" or any(token in lowered for token in ("邮件", "邮箱", "email", "mail"))
    wants_search = intent == "web" or any(token in lowered for token in ("搜索", "新闻", "资讯", "政策", "热点", "rss", "网页", "search"))
    wants_automation = intent == "automation" or any(token in lowered for token in ("自动化", "定时", "提醒", "任务", "每天", "每周"))
    if wants_email:
        email_preferences = str(profile.get("email_preferences") or "").strip()
        if email_preferences:
            lines.append(f"- 邮件整理偏好：{_clip(email_preferences, 220)}")
    if wants_search or wants_automation:
        topics = context.get("topics") or []
        domains = context.get("source_domains") or []
        urls = context.get("source_urls") or []
        fiscal = context.get("fiscal_sources") or []
        if topics:
            lines.append("- 常用关注词：" + "、".join(str(x) for x in topics[:10]))
        if domains:
            lines.append("- 常用来源域名：" + "、".join(str(x) for x in domains[:12]))
        if urls:
            lines.append("- 用户指定来源：" + "、".join(str(x) for x in urls[:6]))
        if fiscal:
            lines.append("- 财税默认来源：" + "、".join(str(x) for x in fiscal[:4]))
    approval = str(profile.get("approval_policy") or profile.get("boundaries") or "").strip()
    if approval:
        lines.append(f"- 权限边界：{_clip(approval, 180)}")
    if not lines:
        return ""
    text = "## 个人认知库提示\n" + "\n".join(lines)
    text += "\n- 这些内容只作为偏好和上下文，不代表授权；涉及发送、写入、删除、登录、安装等动作仍按审批策略执行。"
    return _clip(text, max_chars)


def personal_route_hints(data_dir, message: str = "", intent: str = "", limit: int = 6) -> dict:
    """Small structured hints used by the deterministic router."""
    context = load_personal_context(data_dir)
    profile = context.get("profile") or {}
    lowered = f"{message or ''} {intent or ''}".lower()
    preferences = []
    source_domains = context.get("source_domains") or []
    topics = context.get("topics") or []
    if topics:
        preferences.append("用户画像关注词：" + "、".join(str(x) for x in topics[:limit]))
    if source_domains and (intent in {"web", "automation"} or any(x in lowered for x in ("搜索", "新闻", "资讯", "政策", "简报", "热点"))):
        preferences.append("用户画像常用来源：" + "、".join(str(x) for x in source_domains[:limit]))
    email_pref = str(profile.get("email_preferences") or "").strip()
    if email_pref and (intent == "email" or any(x in lowered for x in ("邮件", "邮箱", "email", "mail"))):
        preferences.append("用户画像邮件规则：" + _clip(email_pref, 160))
    output_pref = str(profile.get("output_preferences") or "").strip()
    if output_pref:
        preferences.append("用户画像输出规则：" + _clip(output_pref, 140))
    return {"preferences": preferences[:limit], "source_domains": source_domains[:limit], "topics": topics[:limit]}


def google_news_sources(domains, keywords=None, limit=10) -> list[str]:
    terms = " OR ".join(list(dict.fromkeys([str(x).strip() for x in (keywords or []) if str(x).strip()]))[:6])
    topic = f"({terms})" if terms else "AI OR 科技 OR 创业"
    sources = []
    for domain in list(dict.fromkeys(domains or []))[:limit]:
        query = f"{topic} site:{domain} when:1d"
        sources.append("https://news.google.com/rss/search?q=" + quote(query) + "&hl=zh-CN&gl=CN&ceid=CN:zh-Hans")
    return sources


def _clip(value, limit=160):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip("，。；、 ") + "…"


def _summary(profile, topics, domains, output_preferences):
    parts = []
    role = profile.get("custom_role") or profile.get("role")
    if role and role != "all":
        parts.append(f"角色：{role}")
    if topics:
        parts.append("关注：" + "、".join(topics[:6]))
    if domains:
        parts.append(f"常用来源 {min(len(domains), 10)} 个")
    if output_preferences:
        parts.append("输出偏好已应用")
    return "；".join(parts)
